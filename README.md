# ChatDSJ Slackbot

A Python backend server that integrates with Slack using Socket Mode.

## Features

- Responds when mentioned in a Slack channel
- Uses randomly selected rude phrases for responses
- Tracks message counts and participants in channels

## Setup

1. Install dependencies:
```bash
poetry install
```

2. Create a Slack app at https://api.slack.com/apps
   - Create from App Manifest option
   - Paste the following manifest JSON:
   ```json
   {
       "display_information": {
           "name": "ChatDSJ Bot",
           "description": "A rude Slackbot that counts messages and tracks participants",
           "background_color": "#2c2d30"
       },
       "features": {
           "bot_user": {
               "display_name": "ChatDSJ Bot",
               "always_online": true
           },
           "app_home": {
               "home_tab_enabled": false,
               "messages_tab_enabled": true,
               "messages_tab_read_only_enabled": false
           }
       },
       "oauth_config": {
           "scopes": {
               "bot": [
                   "app_mentions:read",
                   "channels:history",
                   "chat:write",
                   "users:read"
               ]
           }
       },
       "settings": {
           "event_subscriptions": {
               "bot_events": [
                   "app_mention"
               ]
           },
           "interactivity": {
               "is_enabled": false
           },
           "org_deploy_enabled": false,
           "socket_mode_enabled": true,
           "token_rotation_enabled": false
       }
   }
   ```
   - After creating, go to "Basic Information" to get your Signing Secret
   - Go to "OAuth & Permissions" to get your Bot Token (starts with xoxb-)
   - Go to "Socket Mode" and enable it, then generate an App-Level Token (starts with xapp-)
   - Install the app to your workspace

3. Create a `.env` file with your Slack and OpenAI credentials:
```
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_SIGNING_SECRET=your-signing-secret
SLACK_APP_TOKEN=xapp-your-app-token
OPENAI_API_KEY=your-openai-api-key
```

4. Run the server:
```bash
poetry run uvicorn app.main:app --reload
```

5. Test the bot:
   - Invite the bot to a channel in your Slack workspace
   - Mention the bot by typing @ChatDSJ Bot in the channel
   - The bot should respond with a rude phrase and channel statistics
   - Available commands: `stats`, `cost`, `emojis`, `nicknames`, `nickname [your nickname]`, `resetname`

6. Run the tests:
   ```bash
   poetry run python -m unittest discover -s tests
   ```
