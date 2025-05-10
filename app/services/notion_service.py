import os
import re
import logging
from notion_client import Client, APIResponseError, APIErrorCode
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_USER_DB_ID = os.getenv("NOTION_USER_DB_ID")

notion_client: Optional[Client] = None

if NOTION_API_TOKEN and NOTION_USER_DB_ID:
    try:
        notion_client = Client(auth=NOTION_API_TOKEN)
        logger.info("Notion client initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Notion client: {e}")
else:
    logger.warning(
        "NOTION_API_TOKEN or NOTION_USER_DB_ID not found. "
        "Notion features for user memory will be disabled."
    )

def _get_user_page_id(slack_user_id: str) -> Optional[str]:
    """Helper function to find a user's Notion page ID by Slack User ID."""
    if not notion_client or not NOTION_USER_DB_ID:
        return None
    try:
        response = notion_client.databases.query(
            database_id=NOTION_USER_DB_ID,
            filter={
                "property": "UserID", # This MUST match your Title property name
                "title": { # Querying the 'Title' property type
                    "equals": slack_user_id
                }
            }
        )
        if response.get("results"):
            return response["results"][0]["id"]
    except APIResponseError as e:
        logger.error(f"Notion API Error finding page for user {slack_user_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error finding page for {slack_user_id}: {e}", exc_info=True)
    return None

def get_user_page_properties(slack_user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves all properties of a user's Notion page."""
    if not notion_client: return None
    page_id = _get_user_page_id(slack_user_id)
    if not page_id: return None
    try:
        page_object = notion_client.pages.retrieve(page_id=page_id)
        return page_object.get("properties")
    except APIResponseError as e:
        logger.error(f"Notion API Error fetching page properties for user {slack_user_id}, page_id {page_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching page properties for {slack_user_id}: {e}", exc_info=True)
    return None

def get_user_preferred_name_from_properties(properties: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extracts the preferred name from a page's properties dictionary."""
    if not properties:
        return None
    
    preferred_name_prop_data = properties.get("PreferredName") # Use your exact Notion Property Name
    if preferred_name_prop_data and preferred_name_prop_data.get("type") == "rich_text":
        rich_text_array = preferred_name_prop_data.get("rich_text", [])
        if rich_text_array and len(rich_text_array) > 0:
            return rich_text_array[0].get("plain_text")
    return None

def get_user_notion_page_content(slack_user_id: str) -> Optional[str]:
    """
    Retrieves the concatenated text content from a user's Notion page.
    Designed for Option B where facts/instructions are page content.
    """
    if not notion_client:
        return None

    page_id = _get_user_page_id(slack_user_id)
    if not page_id:
        logger.info(f"No Notion page found for Slack User ID: {slack_user_id} to get content from.")
        return None

    try:
        all_text_parts = []
        has_more = True
        next_cursor = None
        
        logger.info(f"DEBUG: Fetching blocks for page_id: {page_id}") # Changed to INFO for visibility
        while has_more:
            blocks_response = notion_client.blocks.children.list(
                block_id=page_id,
                start_cursor=next_cursor
            )
            results = blocks_response.get("results", [])
            logger.info(f"DEBUG: Fetched {len(results)} blocks. Has more: {blocks_response.get('has_more')}") # Changed to INFO

            for i, block in enumerate(results):
                block_type = block.get("type")
                logger.info(f"DEBUG: Processing block {i}, type: {block_type}")

                # Add 'code' to the list of block types to parse
                if block_type in ["paragraph", "heading_1", "heading_2", "heading_3", 
                                "bulleted_list_item", "numbered_list_item", "code"]: # Added "code"
                    
                    text_element = block.get(block_type, {})
                    current_block_texts = []
                    # For 'code' blocks, the rich_text is directly under the 'code' key.
                    # For other text-bearing blocks, it's usually under block_type -> rich_text.
                    # The Notion client library might abstract this, but the raw API structure is:
                    # 'code': { 'rich_text': [...], 'caption': [...], 'language': '...' }
                    # So, block.get(block_type, {}).get("rich_text", []) should still work if 'code' is text-based.
                    # However, a more direct way for code blocks if block.get('code') exists:
                    
                    rich_text_list_location = text_element.get("rich_text")
                    if block_type == "code" and "rich_text" not in text_element : # Check if rich_text is directly under 'code'
                        # Some Notion API versions or scenarios might have plain text for code blocks
                        # or the rich_text might be nested differently.
                        # Let's assume for now the 'code' block type has 'rich_text' like others based on common patterns.
                        # If it's just plain text, it might be under block['code']['text'] or similar.
                        # The most robust way is to inspect the actual block structure if this doesn't work.
                        # For now, relying on the existing structure:
                        pass # The existing logic below should try to get 'rich_text' from text_element

                    if rich_text_list_location is None and block_type == "code":
                        # Fallback if 'rich_text' isn't directly in block['code']
                        # but is rather block['code']['caption'] or similar, or if it's plain text.
                        # For simple text extraction from code, often the text is in the first rich_text item.
                        # This part might need refinement based on exact code block structure from API.
                        # A 'code' block's primary content is usually within its 'rich_text' array.
                        # If you just pasted markdown, it should be in 'rich_text'.
                        pass


                    for rich_text_item in rich_text_list_location if rich_text_list_location else []:
                        plain_text = rich_text_item.get("plain_text", "")
                        if plain_text:
                            current_block_texts.append(plain_text)
                    
                    if current_block_texts:
                        block_full_text = "".join(current_block_texts)
                        all_text_parts.append(block_full_text)
                        logger.info(f"DEBUG: Extracted from block {i} ({block_type}): '{block_full_text}'")
                else:
                    logger.info(f"DEBUG: Skipping block {i}, type: {block_type} (not in parse list)")


            has_more = blocks_response.get("has_more", False)
            next_cursor = blocks_response.get("next_cursor")

        full_content = "\n".join(filter(None, all_text_parts)).strip() # Join non-empty parts

        if full_content:
            logger.info(f"Retrieved Notion page content for user {slack_user_id} (length: {len(full_content)}).")
            return full_content
        else:
            logger.info(f"Notion page for user {slack_user_id} (ID: {page_id}) is empty or has no parsable text content.")
            return "" # Return empty string instead of None if page exists but is empty
            
    except APIResponseError as e:
        logger.error(f"Notion API Error fetching content for page {page_id} (user {slack_user_id}): {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching content for page {page_id} (user {slack_user_id}): {e}", exc_info=True)
    return None


def store_user_nickname(slack_user_id: str, nickname: str, slack_display_name: Optional[str] = None) -> bool:
    if not notion_client or not NOTION_USER_DB_ID:
        logger.warning("Notion client or User DB ID not configured. Cannot store nickname.")
        return False

    page_id = _get_user_page_id(slack_user_id)

    properties_to_update: Dict[str, Any] = {
        "PreferredName": {"rich_text": [{"type": "text", "text": {"content": nickname}}]}
    }
    if slack_display_name:
        properties_to_update["SlackDisplayName"] = {"rich_text": [{"type": "text", "text": {"content": slack_display_name}}]}

    try:
        if page_id:
            logger.info(f"Updating Notion page {page_id} properties for user {slack_user_id} with nickname: {nickname}")
            notion_client.pages.update(page_id=page_id, properties=properties_to_update)
        else:
            logger.info(f"Creating new Notion page for user {slack_user_id} with nickname property: {nickname}")
            new_page_properties = {
                "UserID": {"title": [{"type": "text", "text": {"content": slack_user_id}}]},
                "PreferredName": {"rich_text": [{"type": "text", "text": {"content": nickname}}]}
            }
            if slack_display_name:
                 new_page_properties["SlackDisplayName"] = {"rich_text": [{"type": "text", "text": {"content": slack_display_name}}]}
            
            # When creating the page, you CAN still add generic initial body content
            # like the "User Facts & Instructions:" heading, but NOT the nickname itself.
            initial_body_content = [
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": { "rich_text": [{"type": "text", "text": {"content": "User Facts & Instructions:"}}]}
                },
                { # Add a placeholder paragraph to encourage editing
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": { "rich_text": [{"type": "text", "text": {"content": "(Edit this page to add specific facts and preferences for the bot to use.)"}}]}
                }
            ]
            notion_client.pages.create(
                parent={"database_id": NOTION_USER_DB_ID},
                properties=new_page_properties,
                children=initial_body_content # Add generic starter content
            )
        return True
    except APIResponseError as e:
        logger.error(f"Notion API Error storing nickname for {slack_user_id}. Status: {e.status}. Code: {e.code}. Body: {e.body}")
    except Exception as e:
        logger.error(f"Unexpected error storing nickname for {slack_user_id}: {e}", exc_info=True)
    return False

# This function would ideally be part of your command parsing logic in app.py
def handle_nickname_command(prompt_text: str, slack_user_id: str, slack_client: Any) -> Optional[str]:
    nickname = None
    
    # Pattern 1: Prioritize quoted names (allows spaces within quotes)
    # Example: "call me \"Il Duderino\""
    quoted_name_pattern = r"(?:call me|my name is|i'm|i am|I am|I'm)\s+\"([a-zA-Z0-9\s'-]+)\""
    match = re.search(quoted_name_pattern, prompt_text, re.IGNORECASE)
    if match:
        nickname = match.group(1)
    
    if not nickname:
        # Pattern 2: Unquoted names, capture everything after the trigger phrase
        # Example: "call me Il Duderino"
        # This will capture "Il Duderino"
        unquoted_name_pattern = r"(?:call me|my name is|i'm|i am|I am|I'm)\s+([a-zA-Z0-9\s'-]+(?: [a-zA-Z0-9\s'-]+)*)"
        match = re.search(unquoted_name_pattern, prompt_text, re.IGNORECASE)
        if match:
            nickname = match.group(1)

    # You could add more patterns here for things like "My nickname is X", etc.
    # For "My nickname is Il Duderino" or "Il Duderino is my nickname"
    if not nickname:
        suffix_pattern = r"([a-zA-Z0-9\s'-]+(?: [a-zA-Z0-9\s'-]+)*)\s*(?:is my name|is my nickname)"
        match = re.search(suffix_pattern, prompt_text, re.IGNORECASE)
        if match:
            nickname = match.group(1)
    
    if nickname:
        nickname = nickname.strip() # General cleanup
        # Further cleanup: remove potential trailing punctuation if the regex is too greedy
        nickname = re.sub(r'[.,!?]$', '', nickname)

        try:
            user_info_response = slack_client.users_info(user=slack_user_id)
            slack_display_name = user_info_response.get("user", {}).get("profile", {}).get("display_name") or \
                                 user_info_response.get("user", {}).get("real_name")
        except Exception as e:
            logger.warning(f"Could not fetch Slack display name for {slack_user_id}: {e}")
            slack_display_name = None

        # Return a tuple: (message_to_say, success_boolean)
        if store_user_nickname(slack_user_id, nickname, slack_display_name=slack_display_name):
            return f"Got it! I'll call you {nickname} from now on. 👍", True
        else:
            return "Hmm, I had a little trouble remembering that nickname. Please try again later.", False
            
    return None, False # Return None for message, False for success if no nickname command