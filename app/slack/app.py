from slack_bolt import App
from slack_sdk.errors import SlackApiError
from slack_bolt.adapter.socket_mode import SocketModeHandler
import os
import random
import re
from openai import OpenAI
import logging
from typing import Optional, Dict, Any, Tuple, List

from datetime import datetime
from collections import defaultdict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
    if not slack_bot_token or not slack_signing_secret:
        raise ValueError("SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET must be set")
    app = App(token=slack_bot_token, signing_secret=slack_signing_secret)
    logger.info("Slack app initialized successfully")
    IS_DUMMY_APP = False
except Exception as e:
    logger.error(f"Failed to initialize Slack app: {e}. Using DummyApp.")
    class DummyApp: # Dummy app to prevent crashes if init fails
        def __init__(self): self.client = None
        def event(self, event_type): return lambda func: func
        def error(self, func): return func
        def message(self, *args, **kwargs): return lambda func: func
        def reaction_added(self, *args, **kwargs): return lambda func: func
    app = DummyApp()
    IS_DUMMY_APP = True

openai_client: Optional[OpenAI] = None
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info(f"OpenAI client initialized successfully for model {OPENAI_MODEL}")
    except Exception as e:
        logger.error(f"Failed to initialize OpenAI client: {e}")
else:
    logger.warning("OPENAI_API_KEY not found. OpenAI features will be disabled.")

channel_data = {} # {channel_id: {"message_count": int, "participants": set(), "last_updated": datetime}}
emoji_tally = defaultdict(int) # {emoji_name: count}
user_nicknames = {} # {user_id: nickname}
bot_user_id: Optional[str] = None
openai_usage_costs = defaultdict(float) # {model_name: cost}
openai_token_counts = defaultdict(lambda: defaultdict(int)) # {model_name: {prompt_tokens: count, ...}}
bot_message_timestamps = set() # Store ts of messages sent by the bot

if not IS_DUMMY_APP:
    try:
        auth_test_result = app.client.auth_test()
        bot_user_id = auth_test_result.get("user_id")
        if bot_user_id: logger.info(f"Bot User ID fetched: {bot_user_id} (Test Bot)")
        else: logger.error("Could not get bot_user_id from auth.test result.")
    except Exception as e: logger.error(f"Error fetching bot user ID: {e}")


RUDE_PHRASES = [
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

def get_random_rude_phrase():
    """Return a randomly selected rude phrase"""
    return random.choice(RUDE_PHRASES)

SYSTEM_PROMPT = os.getenv("OPENAI_SYSTEM_PROMPT", "You are a helpful assistant in a Slack conversation. Be concise.")
MODEL_PRICING = { # Prices per 1M tokens
    "gpt-4o": {"prompt": 5.00, "completion": 15.00},
    "gpt-4-turbo": {"prompt": 10.00, "completion": 30.00},
    "gpt-3.5-turbo-0125": {"prompt": 0.50, "completion": 1.50},
}

def calculate_cost(usage: Dict[str, int], model: str) -> float:
    base_model = model.split('-preview')[0] # Handle potential preview suffixes
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
    user_cache = {}
    if IS_DUMMY_APP or not client: # Basic format if no client
        for msg in reversed(messages):
            if msg.get("type") == "message" and msg.get("text"):
                uid = msg.get("user")
                role = "assistant" if uid == bot_user_id else "user"
                formatted.append({"role": role, "content": f"User {uid}: {msg.get('text', '')}" if uid else msg.get('text', '')})
        return formatted

    for msg in reversed(messages): # Oldest first
        if msg.get("type") != "message" or msg.get("subtype") or not msg.get("text"): continue
        uid = msg.get("user") or msg.get("bot_id")
        text = msg.get("text", "")
        username = "Unknown"
        if uid:
            if uid in user_nicknames:
                username = user_nicknames[uid]
            elif uid in user_cache:
                username = user_cache[uid]
            else:
                try:
                    info = client.users_info(user=uid)
                    udata = info.get("user", {})
                    username = udata.get("real_name", udata.get("name", f"User {uid}"))
                    user_cache[uid] = username
                except SlackApiError as e: # More specific error handling
                    logger.warning(f"Could not fetch user info for {uid} (Slack API Error): {e.response['error']}")
                    username = f"User {uid}"
                    user_cache[uid] = username # Cache fallback
                except Exception as e:
                    logger.warning(f"Could not fetch user info for {uid} (Other Error): {e}")
                    username = f"User {uid}"
                    user_cache[uid] = username # Cache fallback
        role = "assistant" if uid == bot_user_id else "user"
        if text: formatted.append({"role": role, "content": f"{username}: {text}"})
    return formatted

def get_openai_response(hist_openai_fmt: List[Dict[str, str]], prompt: str, web_search: bool = False) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not openai_client: return "My OpenAI brain is offline.", None
    
    try:
        logger.debug(f"Sending request to OpenAI model {OPENAI_MODEL} with web_search={web_search}...")
        
        if web_search:
            conversation_text = ""
            for msg in hist_openai_fmt:
                conversation_text += f"{msg['content']}\n"
            
            input_text = f"{SYSTEM_PROMPT}\n\nConversation history:\n{conversation_text}\n\nCurrent message: {prompt}"
            
            response = openai_client.responses.create(
                model=OPENAI_MODEL,
                tools=[{"type": "web_search_preview"}],
                input=input_text
            )
            
            content = response.output_text
            
            usage = None
            if hasattr(response, 'usage'):
                usage = response.usage.model_dump() if response.usage else None
                if usage: update_usage_tracking(usage, OPENAI_MODEL)
            
            return content, usage
        else:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, *hist_openai_fmt, {"role": "user", "content": prompt}]
            response = openai_client.chat.completions.create(
                model=OPENAI_MODEL, 
                messages=messages, 
                max_tokens=1500
            )
            
            content = response.choices[0].message.content
            usage = response.usage.model_dump() if response.usage else None
            if usage: update_usage_tracking(usage, OPENAI_MODEL)
            
            return content, usage
    except Exception as e:
        logger.error(f"Error getting OpenAI response: {e}")
        return f"Sorry, error processing request with OpenAI: {e}", None

def record_bot_message(say_result: Optional[Dict[str, Any]]):
    if say_result and say_result.get("ok") and say_result.get("ts"):
        bot_message_timestamps.add(say_result["ts"])
        logger.debug(f"Recorded bot message timestamp: {say_result['ts']}")



def update_channel_stats(channel_id, user_id, message_ts):
    """Update the channel statistics"""
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
    """Get the channel statistics"""
    if channel_id not in channel_data:
        return {
            "message_count": 0,
            "participants": set(),
            "last_updated": datetime.now()
        }
    
    return channel_data[channel_id]

@app.event("app_mention")
def handle_mention(event, say, client, logger):
    if IS_DUMMY_APP:
        say("Slack app is not fully initialized. Cannot process mentions.")
        return

    channel_id = event["channel"]
    user_id = event["user"]
    message_ts = event["ts"]
    text = event.get("text", "")
    thread_ts = event.get("thread_ts") # Handle threads

    prompt = re.sub(f"<@{bot_user_id}>", "", text).strip()

    if prompt.lower() == "stats":
        stats = get_channel_stats(channel_id)
        participants_list = ", ".join([f"<@{user}>" for user in stats["participants"]])
        stats_text = (f"Channel Stats ({channel_id}):\n"
                      f"- Messages: {stats['message_count']}\n"
                      f"- Participants ({len(stats['participants'])}): {participants_list}\n"
                      f"- Last Updated: {stats['last_updated']}")
        say(text=stats_text, thread_ts=thread_ts) # Reply in thread if applicable
        return
    if prompt.lower() == "cost":
        cost_text = "OpenAI API Usage Costs:\n"
        if not openai_usage_costs: cost_text += "No usage recorded yet."
        else:
            for model, cost in openai_usage_costs.items():
                cost_text += f"- {model}: ${cost:.6f}\n"
            cost_text += "\nToken Counts:\n"
            for model, counts in openai_token_counts.items():
                cost_text += f"- {model}: Prompt={counts['prompt_tokens']}, Completion={counts['completion_tokens']}, Total={counts['total_tokens']}\n"
        say(text=cost_text, thread_ts=thread_ts)
        return
    if prompt.lower() == "emojis":
        emoji_text = "Emoji Reaction Tally (on my messages):\n"
        if not emoji_tally: emoji_text += "No reactions recorded yet."
        else:
            for emoji, count in emoji_tally.items():
                emoji_text += f"- :{emoji}:: {count}\n"
        say(text=emoji_text, thread_ts=thread_ts)
        return
    if prompt.lower() == "nicknames":
        nick_text = "User Nicknames:\n"
        if not user_nicknames:
            nick_text += "No nicknames set yet."
        else:
            for uid, nickname in user_nicknames.items():
                nick_text += f"- <@{uid}>: {nickname}\n"
        say(text=nick_text, thread_ts=thread_ts)
        return
    if prompt.lower().startswith("nickname "):
        new_nickname = prompt[9:].strip()
        if new_nickname:
            user_nicknames[user_id] = new_nickname
            say(text=f"Your nickname has been set to '{new_nickname}'.", thread_ts=thread_ts)
        else:
            say(text="Please provide a nickname. Usage: `nickname [your nickname]`", thread_ts=thread_ts)
        return
    if prompt.lower() == "resetname":
        if user_id in user_nicknames:
            del user_nicknames[user_id]
            say(text="Your nickname has been reset to your default Slack name.", thread_ts=thread_ts)
        else:
            say(text="You don't have a custom nickname set.", thread_ts=thread_ts)
        return

    update_channel_stats(channel_id, user_id, message_ts)

    use_web_search = True # Enable web search by default

    history = get_channel_history(client, channel_id, limit=1000) # Fetch up to 1000 messages
    history_limit_reached = len(history) == 1000
    history_formatted = format_conversation_history_for_openai(history, client)
    response_text, usage = get_openai_response(history_formatted, prompt, web_search=True)
    if history_limit_reached:
        response_text = "(Note: I could only access the last 1000 messages in this channel for context.)\n\n" + (response_text or "")

    if response_text:
        say_result = say(text=response_text, thread_ts=thread_ts) # Reply in thread if applicable
        record_bot_message(say_result)
    else:
        say(text="Sorry, I couldn't generate a response.", thread_ts=thread_ts)


@app.event("reaction_added")
def handle_reaction_added(event, logger):
    """Handle emoji reactions added to messages."""
    if IS_DUMMY_APP: return # Don't process if app isn't fully initialized

    item_user = event.get("item_user")
    item_ts = event.get("item", {}).get("ts")
    reaction = event.get("reaction")

    if item_user == bot_user_id and item_ts in bot_message_timestamps:
        emoji_tally[reaction] += 1
        logger.info(f"Reaction :{reaction}: added to bot message {item_ts}. Tally: {emoji_tally[reaction]}")



@app.event("message")
def handle_message_events(event, say, client, logger):
    """Handle direct messages and other message events."""
    if IS_DUMMY_APP: return # Don't process if app isn't fully initialized

    channel_type = event.get("channel_type")
    user_id = event.get("user")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts") # Handle threads if DMs support them

    if user_id == bot_user_id or not text or not user_id:
        return

    if channel_type == "im":
        logger.info(f"Received DM from {user_id}: {text}")
        channel_id = event["channel"] # DM channel ID

        if text.lower() == "stats":
            say(text="Sorry, channel stats are not available in DMs.", thread_ts=thread_ts)
            return
        if text.lower() == "cost":
            cost_text = "OpenAI API Usage Costs:\n"
            if not openai_usage_costs: cost_text += "No usage recorded yet."
            else:
                for model, cost in openai_usage_costs.items(): cost_text += f"- {model}: ${cost:.6f}\n"
                cost_text += "\nToken Counts:\n"
                for model, counts in openai_token_counts.items(): cost_text += f"- {model}: Prompt={counts['prompt_tokens']}, Completion={counts['completion_tokens']}, Total={counts['total_tokens']}\n"
            say(text=cost_text, thread_ts=thread_ts)
            return
        if text.lower() == "emojis":
            emoji_text = "Emoji Reaction Tally (on my messages):\n"
            if not emoji_tally: emoji_text += "No reactions recorded yet."
            else:
                for emoji, count in emoji_tally.items(): emoji_text += f"- :{emoji}:: {count}\n"
            say(text=emoji_text, thread_ts=thread_ts)
            return
        if text.lower() == "nicknames":
            nick_text = "User Nicknames:\n"
            if not user_nicknames:
                nick_text += "No nicknames set yet."
            else:
                for uid, nickname in user_nicknames.items():
                    nick_text += f"- <@{uid}>: {nickname}\n"
            say(text=nick_text, thread_ts=thread_ts)
            return
        if text.lower().startswith("nickname "):
            new_nickname = text[9:].strip()
            if new_nickname:
                user_nicknames[user_id] = new_nickname
                say(text=f"Your nickname has been set to '{new_nickname}'.", thread_ts=thread_ts)
            else:
                say(text="Please provide a nickname. Usage: `nickname [your nickname]`", thread_ts=thread_ts)
            return
        if text.lower() == "resetname":
            if user_id in user_nicknames:
                del user_nicknames[user_id]
                say(text="Your nickname has been reset to your default Slack name.", thread_ts=thread_ts)
            else:
                say(text="You don't have a custom nickname set.", thread_ts=thread_ts)
            return

        use_web_search = True # Enable web search by default
        # history_formatted = format_conversation_history_for_openai(history, client)
        response_text, usage = get_openai_response([], text, web_search=True) # No history for DMs for now

        if response_text:
            say_result = say(text=response_text, thread_ts=thread_ts)
            record_bot_message(say_result)
        else:
            say(text="Sorry, I couldn't generate a response.", thread_ts=thread_ts)

    # else:


@app.error
def error_handler(error, body, logger):
    logger.error(f"Error: {error}")
    logger.debug(f"Body: {body}")
