import unittest
import os
import logging
import re
from unittest.mock import MagicMock, patch
from app.slack.app import handle_mention, get_channel_history, format_conversation_history_for_openai as format_conversation_history, get_openai_response, bot_user_id

class TestSlackBot(unittest.TestCase):
    def setUp(self):
        # Configure logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
        
        # Mock Slack event data
        self.event = {
            "channel": "C12345",
            "user": "U12345",
            "ts": "1617984000.000100",
            "text": "@ChatDSJ Can you help me with my Python code?"
        }
        
        # Mock Slack client and say function
        self.mock_client = MagicMock()
        self.mock_say = MagicMock()
        
        # Mock channel history
        self.mock_messages = [
            {"user": "U12345", "text": "Hello everyone", "ts": "1617983900.000100"},
            {"user": "U67890", "text": "How's it going?", "ts": "1617983950.000100"},
            {"user": "U12345", "text": "I'm working on a project", "ts": "1617983980.000100"}
        ]
        
        # Mock user info
        self.mock_user_info = {
            "user": {
                "real_name": "Test User"
            }
        }

    @patch('app.slack.app.get_channel_history')
    @patch('app.slack.app.format_conversation_history_for_openai')
    @patch('app.slack.app.get_openai_response')
    def test_handle_mention_end_to_end(self, mock_get_openai_response, mock_format_conversation, mock_get_channel_history):
        """Test the entire flow from receiving a mention to sending a response"""
        # Setup mocks
        mock_get_channel_history.return_value = self.mock_messages
        mock_format_conversation.return_value = [
            {"role": "user", "content": "Test User: Hello everyone"},
            {"role": "user", "content": "Another User: How's it going?"},
            {"role": "user", "content": "Test User: I'm working on a project"}
        ]
        mock_get_openai_response.return_value = ("I'm having trouble thinking right now. Please try again later.", None)
        
        # Call the function
        handle_mention(self.event, self.mock_say, self.mock_client, self.logger)
        
        # Verify the flow
        mock_get_channel_history.assert_called_once_with(self.mock_client, "C12345", limit=1000)
        mock_format_conversation.assert_called_once_with(self.mock_messages, self.mock_client)
        mock_get_openai_response.assert_called_once()
        self.mock_say.assert_called_once_with(text="I'm having trouble thinking right now. Please try again later.", thread_ts=None)
    
    def test_real_slack_bot_functionality(self):
        """Test the actual Slack bot functionality with real components"""
        # This test simulates the real-world conditions by using the actual functions
        # but with mocked Slack client to avoid making real Slack API calls
        
        # Setup mocks for Slack API calls
        self.mock_client.conversations_history.return_value = {"messages": self.mock_messages}
        self.mock_client.users_info.return_value = self.mock_user_info
        
        # Get channel history using the real function
        messages = get_channel_history(self.mock_client, "C12345")
        self.assertEqual(messages, self.mock_messages)
        
        # Format conversation history using the real function
        conversation_history = format_conversation_history(messages, self.mock_client)
        self.assertIsInstance(conversation_history, list)
        if conversation_history:
            self.assertIsInstance(conversation_history[0], dict)
            self.assertIn("role", conversation_history[0])
            self.assertIn("content", conversation_history[0])
        
        prompt = re.sub(f"<@{bot_user_id}>", "", self.event["text"]).strip()
        response_text, usage = get_openai_response(conversation_history, prompt, web_search=True)
        
        self.assertNotEqual(response_text, "I'm having trouble thinking right now. Please try again later.")
        self.assertIsNotNone(response_text)
        self.assertTrue(len(response_text) > 0)
        
        # Log the response for debugging
        self.logger.info(f"ChatGPT Response: {response_text}")
        
        # Now simulate the full handle_mention flow with our mocks
        with patch('app.slack.app.get_channel_history', return_value=messages):
            with patch('app.slack.app.format_conversation_history_for_openai', return_value=conversation_history):
                with patch('app.slack.app.get_openai_response', return_value=(response_text, usage)):
                    # Call the actual handle_mention function
                    handle_mention(self.event, self.mock_say, self.mock_client, self.logger)
                    
                    self.assertTrue(self.mock_say.called)
                    if self.mock_say.call_args and self.mock_say.call_args[0]:
                        call_args = self.mock_say.call_args[0][0]
                        self.assertNotEqual(call_args, "I'm having trouble thinking right now. Please try again later.")

if __name__ == '__main__':
    unittest.main()
