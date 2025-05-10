import os
import random
import re
import logging
from datetime import datetime
from collections import defaultdict
from typing import Optional, Dict, Any, Tuple, List

from slack_bolt import App
from slack_sdk.errors import SlackApiError
from openai import OpenAI
from app.services.notion_service import (
    get_user_notion_page_content,
    handle_nickname_command,
    get_user_page_properties,             # New import
    get_user_preferred_name_from_properties # New import
)

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global flags and objects
IS_DUMMY_APP = False
openai_client: Optional[OpenAI] = None
bot_user_id: Optional[str] = None

# Shared memory
channel_data = {}
emoji_tally = defaultdict(int)
openai_usage_costs = defaultdict(float)
openai_token_counts = defaultdict(lambda: defaultdict(int))
bot_message_timestamps = set()

RUDE_PHRASES = [ # This list is defined but not currently used in the bot's responses.
    "Do I look like I care?",
    "Not this again...",
    "Are you seriously bothering me right now?",
    "I have better things to do than talk to you.",
    "Oh great, another pointless conversation.",
    "Whatever. I'm busy.",
    "That's the dumbest thing I've heard all day.",
    "Can you not?",
    "Ugh, what now?",
    "You again? Seriously?"
]

# --- FIX 1: Ensure the correct, detailed SYSTEM_PROMPT is used ---
# The single, authoritative definition of SYSTEM_PROMPT.
# The previous duplicate definition that overwrote this has been removed.
SYSTEM_PROMPT = os.getenv(
    "OPENAI_SYSTEM_PROMPT",
    "You are an assistant embedded in a Slack channel. Your primary job is to answer the user's most recent question directly and concisely. "
    "Review the provided message history to understand the immediate context of the user's question. "
    "If the user's question is *explicitly about past discussions* in the channel (e.g., 'was X discussed before?', 'what did Y say about Z?'), "
    "then you should thoroughly search the history to answer. When specifically asked if a topic was discussed previously, "
    "you must explicitly look for *any* messages referencing it, even once, in the history. "
    "For general queries like 'has any X been discussed?', try to identify substantive discussions first, but also consider brief mentions if no clear discussion is found."
    "For all other questions, prioritize the current user's direct query. "
    "If the user offers a compliment or engages in simple social interaction, respond politely and briefly (e.g., 'Thank you!', 'You're welcome!')."
)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

MODEL_PRICING = { # Prices per 1M tokens
    "gpt-4o": {"prompt": 5.00, "completion": 15.00},
    "gpt-4-turbo": {"prompt": 10.00, "completion": 30.00},
    "gpt-3.5-turbo-0125": {"prompt": 0.50, "completion": 1.50},
}

def create_slack_app():
    global openai_client, bot_user_id, IS_DUMMY_APP

    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_signing_secret = os.environ.get("SLACK_SIGNING_SECRET")

    if not slack_bot_token or not slack_signing_secret:
        logger.warning("Missing SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET — using DummyApp.")
        IS_DUMMY_APP = True

        class DummyApp:
            def __init__(self): self.client = None
            def event(self, *args, **kwargs): return lambda f: f
            def error(self, f): return f
            def message(self, *args, **kwargs): return lambda f: f
            def reaction_added(self, *args, **kwargs): return lambda f: f

        return DummyApp()

    logger.info("Slack app initialized successfully.")
    app = App(token=slack_bot_token, signing_secret=slack_signing_secret)

    try:
        auth_test = app.client.auth_test()
        bot_user_id = auth_test.get("user_id")
        logger.info(f"Bot User ID: {bot_user_id}")
    except Exception as e:
        logger.warning(f"Failed to fetch bot user ID: {e}")

    if OPENAI_API_KEY:
        try:
            openai_client = OpenAI(api_key=OPENAI_API_KEY)
            logger.info("OpenAI client initialized.")
        except Exception as e:
            logger.warning(f"Failed to init OpenAI client: {e}")
    else:
        logger.warning("No OPENAI_API_KEY found — OpenAI features disabled.")

    @app.event("reaction_added")
    def handle_reaction_added(event, logger):
        if IS_DUMMY_APP: return

        item_user = event.get("item_user")
        item_ts = event.get("item", {}).get("ts")
        reaction = event.get("reaction")

        if item_user == bot_user_id and item_ts in bot_message_timestamps:
            emoji_tally[reaction] += 1
            logger.info(f"Reaction :{reaction}: added to bot message {item_ts}. Tally: {emoji_tally[reaction]}")

    @app.event("app_mention")
    def handle_mention(event, say, client, logger):
        if IS_DUMMY_APP:
            say("Slack app is not fully initialized. Cannot process mentions.")
            return

        channel_id = event["channel"]
        user_id = event["user"]
        message_ts = event["ts"]
        text = event.get("text", "")
        reply_thread_ts = event.get("thread_ts", message_ts) 

         # Remove bot mention to get the clean prompt
        bot_mention_pattern = f"<@{bot_user_id}>"
        prompt = re.sub(bot_mention_pattern, "", text).strip()

        is_new_main_channel_question = not event.get("thread_ts") # True if not in a thread

        # Milestone 1 & 2: Ephemeral acknowledgment
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="I heard you! I'm working on a response... 🧠"
            )
        except Exception as e:
            logger.warning(f"Failed to send ephemeral message: {e}")

        # --- MILESTONE 3: Handle Nickname Command ---
        # The functions from notion_service will handle checks for notion_client internally
        user_notion_context = get_user_notion_page_content(user_id) # Directly call the function

        if user_notion_context: # Check the *result* of the function call
            logger.info(f"Fetched Notion context for user {user_id} (length: {len(user_notion_context)}).")
        # If get_user_notion_page_content returns None (e.g. client not init'd or page not found)
        # it will be None here. If it returns an empty string for an empty page, that's also fine.
        elif user_notion_context is None: # Explicitly checking for None from service if client failed
             logger.info(f"Could not fetch Notion context for user {user_id} (Notion client issue or page not found).")
             user_notion_context = "" # Default to empty string for prompt formatting
        else: # user_notion_context is "" (empty page)
            logger.info(f"No Notion context found for user {user_id} or page is empty.")
            # user_notion_context is already ""

        # Check for nickname command BEFORE other processing if it's a direct command
        # This uses the 'client' passed by Slack Bolt to handle_mention
        nickname_response_message, nickname_stored_successfully = handle_nickname_command(prompt, user_id, client) 
        if nickname_response_message:
            say(text=nickname_response_message, thread_ts=reply_thread_ts)
            if nickname_stored_successfully:
                 update_channel_stats(channel_id, user_id, message_ts) 
                 return

        # Fetch full channel history (up to a practical limit)
        def fetch_channel_history_internal(channel_id_param, client_param, limit=1000):
            all_messages = []
            cursor = None
            logger.info(f"Fetching channel history for {channel_id_param} (limit: {limit})...")
            while True:
                try:
                    result = client_param.conversations_history(
                        channel=channel_id_param,
                        limit=min(200, limit - len(all_messages)), # Fetch in chunks, respect overall limit
                        cursor=cursor
                    )
                    messages = result.get("messages", [])
                    all_messages.extend(messages)
                    cursor = result.get("response_metadata", {}).get("next_cursor")
                    if not cursor or len(all_messages) >= limit:
                        break
                except Exception as e:
                    logger.error(f"Failed to fetch chunk of channel history for {channel_id_param}: {e}")
                    break
            logger.info(f"Fetched {len(all_messages)} messages from channel {channel_id_param}.")
            return all_messages[:limit]

        # Fetch full thread history if this is part of one
        def fetch_thread_history_internal(channel_id_param, thread_ts_param, client_param, limit=1000): # Added limit for safety
            all_replies = []
            cursor = None
            logger.info(f"Fetching thread history for {channel_id_param}, ts: {thread_ts_param} (limit: {limit})...")
            while True:
                try:
                    result = client_param.conversations_replies(
                        channel=channel_id_param,
                        ts=thread_ts_param,
                        limit=min(200, limit - len(all_replies)), # Fetch in chunks
                        cursor=cursor
                    )
                    replies = result.get("messages", [])
                    all_replies.extend(replies)
                    # The first message in replies is the parent message of the thread.
                    # Subsequent messages are the actual replies.
                    cursor = result.get("response_metadata", {}).get("next_cursor")
                    if not cursor or len(all_replies) >= limit:
                        break
                except Exception as e:
                    logger.error(f"Failed to fetch chunk of thread replies for {thread_ts_param}: {e}")
                    break
            logger.info(f"Fetched {len(all_replies)} messages from thread {thread_ts_param}.")
            return all_replies[:limit]

         # --- MILESTONE 3: Fetch user-specific context from Notion ---
        user_page_body_content = get_user_notion_page_content(user_id) # Gets content like "## Interests..."
        user_properties = get_user_page_properties(user_id) # Gets all page properties
        preferred_name = get_user_preferred_name_from_properties(user_properties)

        # Construct the final user_specific_context for the LLM
        user_context_parts = []
        if preferred_name:
            user_context_parts.append(f"The user's preferred name is: {preferred_name}.")
            logger.info(f"Using preferred name from Notion property: {preferred_name}")
        
        if user_page_body_content and user_page_body_content.strip():
            user_context_parts.append(f"Other known facts and preferences for this user:\n{user_page_body_content.strip()}")
            logger.info(f"Fetched Notion page body context for user {user_id} (length: {len(user_page_body_content)}).")
        
        final_user_specific_context = "\n".join(user_context_parts)
        if not final_user_specific_context:
            logger.info(f"No specific Notion context (name or page body) found for user {user_id}.")
            final_user_specific_context = "" # Ensure empty string if nothing found

        # Milestone 1 & 2: Fetch relevant histories
        channel_history_messages: List[Dict[str, Any]] = []
        thread_history_messages: List[Dict[str, Any]] = []

        # Keywords indicating a request about past discussions
        history_query_keywords = [
            "discussed", "discussion", "mentioned", "talked about", "said about",
            "summarize", "summary", "what was said", "history of", "previously on"
        ]

        # Default limit for channel history
        channel_history_limit = 1000 # Default to extensive history

        if is_new_main_channel_question:
            # Check if the new main channel question is asking about past history
            is_querying_history = any(keyword.lower() in prompt.lower() for keyword in history_query_keywords)

            if not is_querying_history:
                # If it's a new question NOT asking about past history, limit channel context
                # to avoid bleed-through from unrelated recent threads.
                logger.info("New main channel question NOT about past history. Fetching limited channel history.")
                channel_history_limit = 50 # Or another tuned small number
            else:
                # If it's a new question explicitly asking about past history, fetch more.
                logger.info("New main channel question IS about past history. Fetching extensive channel history.")
                # channel_history_limit remains 1000 (or your preferred extensive limit)

            channel_history_messages = fetch_channel_history_internal(channel_id, client, limit=channel_history_limit)
            # thread_history_messages remains empty for new main channel questions
        else:
            # This question is a reply within an existing thread
            logger.info("Question is within a thread. Fetching extensive channel and specific thread history.")
            channel_history_messages = fetch_channel_history_internal(channel_id, client, limit=1000)
            if event.get("thread_ts"):
                thread_history_messages = fetch_thread_history_internal(channel_id, event["thread_ts"], client, limit=1000)
            else:
                logger.warning("Context indicates a thread, but event['thread_ts'] is missing.")

        # Merge and deduplicate
        merged_messages = []
        if thread_history_messages: # Prioritize thread history if available
            thread_message_timestamps = {msg["ts"] for msg in thread_history_messages}
            merged_messages.extend(thread_history_messages)
            # Add channel messages not already in the thread history
            for msg in channel_history_messages:
                if msg["ts"] not in thread_message_timestamps:
                    merged_messages.append(msg)
        else: # No thread history (e.g., new main channel question)
            merged_messages = channel_history_messages

        logger.info(f"Total messages for context after merging/deduplication: {len(merged_messages)}")

        formatted_history = format_conversation_history_for_openai(merged_messages, client)
        logger.info(f"Formatted OpenAI history contains {len(formatted_history)} segments.")
        if formatted_history:
            for i, msg_seg in enumerate(formatted_history[-5:], 1): # Log last 5 segments
                logger.debug(f"[OpenAI History Sample {i}] Role: {msg_seg['role']} | Content: {str(msg_seg['content'])[:100]}")
        else:
            logger.info("Formatted OpenAI history is empty.")


        # Milestone 1 & 2: Send to OpenAI
        try:
            logger.info(f"Sending prompt to OpenAI: '{prompt}'")
            # Pass the is_new_main_channel_question if you implement suggestion #3 from previous advice
            response_text, usage = get_openai_response(
                hist_openai_fmt=formatted_history,
                prompt=prompt,
                user_specific_context=final_user_specific_context # Pass the combined context
            )

            if response_text:
                logger.info(f"OpenAI returned response (length: {len(response_text)} chars)")
            else:
                logger.warning("OpenAI did not return a response text.")

        except Exception as e:
            logger.error(f"OpenAI call failed: {e}", exc_info=True)
            say(text=f"Oops! I encountered an issue while processing your request with my AI brain: {e}", thread_ts=thread_ts)
            return

        # Milestone 1 & 2: Post response back
        if response_text:
            # Use reply_thread_ts to ensure response goes to the correct place
            say_result = say(text=response_text, thread_ts=reply_thread_ts)
            record_bot_message(say_result)
        else:
            # This case might occur if OpenAI returns an empty content string but no exception.
            say(text="I'm sorry, I couldn't generate a response for that.", thread_ts=thread_ts)

        update_channel_stats(channel_id, user_id, message_ts)

    @app.error
    def error_handler(error, body, logger):
        logger.error(f"Slack App Error: {error}")
        logger.debug(f"Request Body: {body}")

    return app

def get_random_rude_phrase():
    """Return a randomly selected rude phrase"""
    return random.choice(RUDE_PHRASES)

def calculate_cost(usage: Dict[str, int], model: str) -> float:
    base_model = model.split('-preview')[0]
    pricing = MODEL_PRICING.get(base_model)
    if not pricing or not usage: return 0.0
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cost = ((prompt_tokens / 1_000_000) * pricing["prompt"] +
            (completion_tokens / 1_000_000) * pricing["completion"])
    return cost

def update_usage_tracking(usage: Dict[str, int], model: str):
    if not usage: return
    cost = calculate_cost(usage, model)
    openai_usage_costs[model] += cost
    openai_token_counts[model]["prompt_tokens"] += usage.get("prompt_tokens", 0)
    openai_token_counts[model]["completion_tokens"] += usage.get("completion_tokens", 0)
    openai_token_counts[model]["total_tokens"] += usage.get("total_tokens", 0)
    logger.info(f"Updated usage for {model}. Request cost: ${cost:.6f}. Cumulative cost: ${openai_usage_costs[model]:.6f}")

# This function seems potentially redundant given the inline fetch_channel_history_internal
# If not used elsewhere, consider removing or consolidating.
def get_channel_history(client, channel_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    if IS_DUMMY_APP or not client: return []
    try:
        result = client.conversations_history(channel=channel_id, limit=limit)
        return result.get("messages", [])
    except Exception as e:
        logger.error(f"Error fetching channel history for {channel_id}: {e}")
        return []

def format_conversation_history_for_openai(messages: List[Dict[str, Any]], client) -> List[Dict[str, str]]:
    formatted = []
    user_cache = {} # Cache user info to reduce API calls
    if IS_DUMMY_APP or not client:
        for msg in reversed(messages): # Process oldest first for chronological order
            if msg.get("type") == "message" and msg.get("text"):
                uid = msg.get("user")
                role = "assistant" if uid == bot_user_id else "user"
                formatted.append({"role": role, "content": f"User {uid if uid else 'Unknown'}: {msg.get('text', '')}"})
        return formatted

    # Process messages in reverse to get chronological order for OpenAI (oldest first)
    for msg_data in reversed(messages):
        if msg_data.get("type") != "message" or msg_data.get("subtype") or not msg_data.get("text"):
            # Skip non-message types, subtypes (like channel_join), or messages without text
            continue

        user_id_slack = msg_data.get("user") or msg_data.get("bot_id") # Slack user ID or bot ID
        text_content = msg_data.get("text", "")
        username_display = "Unknown User"

        if user_id_slack:
            if user_id_slack in user_cache:
                username_display = user_cache[user_id_slack]
            else:
                try:
                    user_info_response = client.users_info(user=user_id_slack)
                    user_profile = user_info_response.get("user", {})
                    username_display = user_profile.get("real_name", user_profile.get("name", f"User {user_id_slack}"))
                    user_cache[user_id_slack] = username_display
                except SlackApiError as e:
                    logger.warning(f"Could not fetch user info for {user_id_slack} (Slack API Error): {e.response['error']}")
                    username_display = f"User {user_id_slack}" # Fallback
                    user_cache[user_id_slack] = username_display # Cache fallback
                except Exception as e:
                    logger.warning(f"Could not fetch user info for {user_id_slack} (Other Error): {e}")
                    username_display = f"User {user_id_slack}" # Fallback
                    user_cache[user_id_slack] = username_display # Cache fallback
        
        role_for_openai = "assistant" if user_id_slack == bot_user_id else "user"

        if text_content:
            if role_for_openai == "assistant":
                formatted.append({"role": role_for_openai, "content": text_content}) # Just the bot's message
            else:
                formatted.append({"role": role_for_openai, "content": f"{username_display}: {text_content}"})
    
    return formatted

# --- FIX 2: Correct way to pass history to OpenAI ---

def get_openai_response(
    hist_openai_fmt: List[Dict[str, str]],
    prompt: str,
    user_specific_context: Optional[str] = None, # Added argument
    linked_notion_content: Optional[str] = None, # For M4
    web_search: bool = False 
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not openai_client:
        logger.warning("OpenAI client not initialized. Cannot get response.")
        return "My OpenAI brain is offline. Please check configuration.", None

    try:
        system_prompt_content = SYSTEM_PROMPT # Your base system prompt

        if user_specific_context and user_specific_context.strip(): # Check if not empty
            system_prompt_content += (
                f"\n\n--- USER-SPECIFIC CONTEXT & PREFERENCES ---\n"
                f"{user_specific_context.strip()}\n"
                f"--- END USER-SPECIFIC CONTEXT & PREFERENCES ---"
            )
        
        if linked_notion_content and linked_notion_content.strip(): # For M4
            system_prompt_content += (
                f"\n\n--- REFERENCED NOTION PAGES CONTENT ---\n"
                f"{linked_notion_content.strip()}\n"
                f"--- END REFERENCED NOTION PAGES CONTENT ---"
            )

        messages_for_openai = [{"role": "system", "content": system_prompt_content}]
        messages_for_openai.extend(hist_openai_fmt)
        messages_for_openai.append({"role": "user", "content": prompt})

        logger.info(f"Sending {len(messages_for_openai)} message segments to OpenAI. Model: {OPENAI_MODEL}")
        if messages_for_openai: # Log the last message (user prompt)
             logger.debug(f"Last message to OpenAI (user prompt): {messages_for_openai[-1]['content'][:200]}")

        logger.info(f"SYSTEM PROMPT CONTENT BEING SENT TO OPENAI:\n{system_prompt_content}")
        logger.info(f"First 2 messages for OpenAI: {messages_for_openai[:2]}")
        logger.info(f"Last 2 messages for OpenAI: {messages_for_openai[-2:]}")

        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages_for_openai,
            max_tokens=1500
        )
        content = response.choices[0].message.content
        usage = response.usage.model_dump() if response.usage else None
        if usage:
            update_usage_tracking(usage, OPENAI_MODEL)
        return content, usage

    except Exception as e:
        logger.error(f"Error getting OpenAI response: {e}", exc_info=True)
        return f"Sorry, there was an error communicating with OpenAI: {e}", None

def record_bot_message(say_result: Optional[Dict[str, Any]]):
    if say_result and say_result.get("ok") and say_result.get("ts"):
        bot_message_timestamps.add(say_result["ts"])
        logger.debug(f"Recorded bot message timestamp: {say_result['ts']}")

def update_channel_stats(channel_id, user_id, message_ts):
    if channel_id not in channel_data:
        channel_data[channel_id] = {
            "message_count": 0,
            "participants": set(),
            "last_updated": datetime.now()
        }
    
    channel_data[channel_id]["message_count"] += 1
    channel_data[channel_id]["participants"].add(user_id)
    channel_data[channel_id]["last_updated"] = datetime.now()

def get_channel_stats(channel_id):
    if channel_id not in channel_data:
        return {
            "message_count": 0,
            "participants": set(),
            "last_updated": datetime.now()
        }
    return channel_data[channel_id]

if __name__ == "__main__":
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    app = create_slack_app()
    if not IS_DUMMY_APP and "SLACK_APP_TOKEN" in os.environ:
        logger.info("Starting SocketModeHandler for Slack Bot...")
        SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
    elif IS_DUMMY_APP:
        logger.warning("App running in dummy mode. No SocketModeHandler started.")
    else:
        logger.error("SLACK_APP_TOKEN not found. Cannot start SocketModeHandler.")