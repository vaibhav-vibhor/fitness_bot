# 🏃‍♂️ AI-Powered Health Analysis Dashboard

An intelligent health monitoring application that connects with Fitbit to analyze your fitness data and provides personalized health insights using Google's Gemini AI.

## ✨ Features

- **Fitbit Integration**: Securely connects to your Fitbit account to fetch health data
- **AI-Powered Analysis**: Uses Google Gemini AI to analyze your health metrics and provide insights
- **Interactive Dashboard**: Beautiful web interface with clickable health score cards
- **Comprehensive Health Scoring**: Analyzes activity, heart health, sleep, and body metrics
- **Smart Caching**: Caches results for 24 hours to minimize API calls
- **Rate Limiting**: Built-in protection against API rate limits

## 🛠️ What You'll Need

### Required Accounts & APIs
1. **Fitbit Developer Account** - To access your fitness data
2. **Google AI Studio Account** - For Gemini AI analysis
3. **Python 3.8+** - To run the application

### System Requirements
- Windows, macOS, or Linux
- Python 3.8 or higher
- Internet connection for API calls

## 📋 Installation & Setup

### Step 1: Download the Project
```bash
# Clone or download this repository
git clone [your-repository-url]
cd health_analysis
```

### Step 2: Set Up Python Environment
```bash
# Create a virtual environment (recommended)
python -m venv .venv

# Activate it:
# On Windows:
.venv\Scripts\activate
# On Mac/Linux:
source .venv/bin/activate
```

### Step 3: Install Required Packages
```bash
pip install flask requests python-dotenv google-generativeai
```

### Step 4: Get Your API Keys

#### Fitbit API Setup:
1. Go to [Fitbit Developer Console](https://dev.fitbit.com/apps)
2. Click "Register New App"
3. Fill in the details:
   - **Application Name**: Your app name (e.g., "Health Analysis Dashboard")
   - **Description**: Any description
   - **Application Website**: `http://localhost:3000`
   - **Organization**: Your name/organization
   - **Organization Website**: Any website
   - **OAuth 2.0 Application Type**: Select "Personal"
   - **Callback URL**: `http://localhost:3000/callback`
   - **Default Access Type**: Read Only
4. Save your **Client ID** and **Client Secret**

#### Google Gemini API Setup:
1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Click "Create API Key"
3. Save your **API Key**

### Step 5: Configure Environment Variables
1. Copy the example environment file:
   ```bash
   copy .env.example .env    # Windows
   cp .env.example .env      # Mac/Linux
   ```

2. Open `.env` file and fill in your details:
   ```bash
   # Your Fitbit credentials from Step 4
   CLIENT_ID=your_actual_fitbit_client_id
   CLIENT_SECRET=your_actual_fitbit_client_secret
   
   # Your Gemini API key from Step 4
   GEMINI_API_KEY=your_actual_gemini_api_key
   
   # Generate a random secret key (keep this secure!)
   SECRET_KEY=your-very-long-random-secret-key-at-least-32-characters
   
   # Leave these as they are
   FLASK_ENV=development
   DEBUG=True
   ```

### Step 6: Run the Application
```bash
python auth.py
```

You should see:
```
* Running on http://127.0.0.1:3000
* Debug mode: on
```

### Step 7: Open in Your Browser
Go to: `http://localhost:3000`

## 🚀 How to Use

1. **First Visit**: Click "Connect with Fitbit" to authorize the app
2. **Login to Fitbit**: Use your Fitbit credentials
3. **Wait for Analysis**: The app will fetch your data and generate health insights (this may take a minute)
4. **Explore Your Dashboard**: 
   - View your overall health score
   - Click on any health metric card to see detailed breakdowns
   - Check individual categories like activity, heart health, sleep, and body metrics

## 📁 Project Structure

```
health_analysis/
├── auth.py                     # Main Flask application
├── gemini_health_analyzer.py   # AI analysis logic
├── health_score_cache.py       # Caching system
├── templates/                  # HTML templates
│   ├── index.html             # Landing page
│   ├── dashboard.html         # Main dashboard
│   └── [other templates]     # Individual metric pages
├── cache/                     # Cached health scores
├── .env                       # Your API keys (not committed to git)
├── .env.example              # Template for environment variables
└── .gitignore                # Excludes sensitive files from git
```

## 🔧 Troubleshooting

### Common Issues:

**1. "Module not found" errors**
```bash
# Make sure your virtual environment is activated and install packages:
pip install flask requests python-dotenv google-generativeai
```

**2. "Invalid client credentials" error**
- Double-check your Fitbit Client ID and Client Secret in `.env`
- Make sure callback URL in Fitbit app settings is exactly: `http://localhost:3000/callback`

**3. "Invalid API key" for Gemini**
- Verify your Gemini API key in `.env`
- Make sure you've enabled the Gemini API in Google AI Studio

**4. Port 3000 already in use**
```bash
# Find what's using port 3000:
netstat -ano | findstr :3000    # Windows
lsof -i :3000                   # Mac/Linux

# Kill the process or change port in auth.py (last line)
```

**5. No health data showing**
- Make sure you have recent data in your Fitbit account
- Wait a few minutes for data to sync from your Fitbit device
- Try refreshing the page

### Rate Limiting
The app has built-in protection against Fitbit's API rate limits (150 requests/hour). If you see rate limit messages, just wait a few minutes and try again.

## 🔒 Security & Privacy

- **Local Only**: Your data stays on your computer
- **Secure Storage**: API keys are stored in environment variables
- **24-Hour Cache**: Health scores are cached locally to minimize API calls
- **No Data Sharing**: No personal data is sent anywhere except to official Fitbit and Google APIs

## 🤝 Contributing

Feel free to submit issues and enhancement requests!

## 📄 License

This project is for educational and personal use.

## ⚠️ Important Notes

- Keep your `.env` file secure and never share it
- The app runs locally on your computer
- Your Fitbit and health data never leaves your machine (except for official API calls)
- Health scores are cached for 24 hours to improve performance

---

**Need Help?** If you run into issues, check the troubleshooting section above or review the error messages in your terminal - they usually point to what's wrong!