import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY environment variable not set.")

client = OpenAI(api_key=api_key)
MODEL = "gpt-4o" # Default model, can be overridden

def set_openai_model(model_name: str):
    """Sets the OpenAI model to be used."""
    global MODEL
    MODEL = model_name
    print(f"OpenAI model set to: {MODEL}")

from typing import Optional, List, Dict, Tuple, Any

def get_chat_completion(prompt: str, conversation_history: Optional[List[Dict[str, str]]] = None, use_web_search: bool = False) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Gets a chat completion from OpenAI.

    Args:
        prompt (str): The user's message/prompt.
        conversation_history (list, optional): List of previous messages for context. Defaults to None.
        use_web_search (bool, optional): Whether to enable the web search tool. Defaults to False.

    Returns:
        tuple[str, dict]: A tuple containing the response text and the usage statistics.
                          Returns (None, None) if an error occurs.
    """
    messages = [
        {"role": "system", "content": "You are a helpful participant in a Slack conversation. Be concise and informative."},
    ]
    if conversation_history:
        messages.extend(conversation_history)
    instruction = (
        f"Use the context above to respond to the user's last message: \"{prompt}\".\n"
        f"Provide a concise, helpful response as if participating in the same Slack conversation."
    )
    messages.append({"role": "user", "content": instruction})

    tools = []
    if use_web_search:
        tools.append({
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for current information",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        })

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools if tools else None,
        )
        
        content = response.choices[0].message.content
        usage = response.usage # Returns an object like {'prompt_tokens': 57, 'completion_tokens': 40, 'total_tokens': 97}
        

        return content, usage.model_dump() if usage else None

    except Exception as e:
        print(f"Error getting OpenAI completion: {e}")
        return None, None # type: ignore

if __name__ == "__main__":
    test_prompt = "What's the weather like in San Francisco today?"
    print(f"Testing with prompt: {test_prompt}")
    
    print("\n--- Test without web search ---")
    response_text, usage_info = get_chat_completion(test_prompt)
    if response_text:
        print(f"Response: {response_text}")
        print(f"Usage: {usage_info}")
    else:
        print("Failed to get response.")

    print("\n--- Test with web search ---")
    response_text_ws, usage_info_ws = get_chat_completion(test_prompt, use_web_search=True)
    if response_text_ws:
        print(f"Response (with web search): {response_text_ws}")
        print(f"Usage (with web search): {usage_info_ws}")
    else:
        print("Failed to get response with web search.")
