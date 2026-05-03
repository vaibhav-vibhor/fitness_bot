from flask import Flask, redirect, request, session, render_template, url_for, jsonify
import requests, base64, time
import os
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
import threading
import uuid
from gemini_health_analyzer import get_health_analyzer
from health_score_cache import get_health_score_cache, get_user_id_from_session
import random
from functools import wraps
load_dotenv()

# Environment configuration
IS_PRODUCTION = os.environ.get('FLASK_ENV') == 'production'

app = Flask(__name__)
# Generate a secure secret key if not provided in environment
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())

# Decorator to protect debug/test endpoints in production
def debug_only(f):
    """Decorator to disable endpoints in production"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if IS_PRODUCTION:
            from flask import abort
            abort(404)
        return f(*args, **kwargs)
    return decorated_function

# Rate limiting class to manage Fitbit API calls
class FitbitRateLimiter:
    def __init__(self, requests_per_hour=150, burst_limit=10):
        self.requests_per_hour = requests_per_hour
        self.burst_limit = burst_limit
        self.requests_made = []
        self.last_request_time = 0
        self.lock = threading.Lock()
        self.backoff_delay = 0
    
    def wait_if_needed(self):
        with self.lock:
            current_time = time.time()
            
            # Apply exponential backoff if we recently hit a rate limit
            if self.backoff_delay > 0:
                time.sleep(self.backoff_delay)
                self.backoff_delay = max(1, self.backoff_delay * 0.5)  # Reduce backoff gradually
            
            # Clean old requests (older than 1 hour)
            self.requests_made = [req_time for req_time in self.requests_made if current_time - req_time < 3600]
            
            # Check burst limit (requests per minute)
            recent_requests = [req_time for req_time in self.requests_made if current_time - req_time < 60]
            
            # Minimum delay between requests
            min_delay = 0.5 + random.uniform(0, 0.5)  # 0.5-1 second base delay + jitter
            time_since_last = current_time - self.last_request_time
            if time_since_last < min_delay:
                time.sleep(min_delay - time_since_last)
            
            # Check if we need to wait for burst limit
            if len(recent_requests) >= self.burst_limit:
                wait_time = 60 - (current_time - recent_requests[0]) + random.uniform(1, 3)
                print(f"⏳ Rate limit: waiting {wait_time:.1f}s for burst limit")
                time.sleep(wait_time)
            
            # Check hourly limit
            if len(self.requests_made) >= self.requests_per_hour:
                wait_time = 3600 - (current_time - self.requests_made[0]) + random.uniform(10, 30)
                print(f"⏳ Rate limit: waiting {wait_time:.1f}s for hourly limit")
                time.sleep(wait_time)
            
            # Record this request
            self.requests_made.append(time.time())
            self.last_request_time = time.time()
    
    def handle_rate_limit_error(self):
        with self.lock:
            # Exponential backoff starting at 5 seconds, max 60 seconds
            self.backoff_delay = min(60, max(5, self.backoff_delay * 2)) if self.backoff_delay > 0 else 5
            print(f"🚫 Rate limit hit! Setting backoff delay to {self.backoff_delay}s")

# Global rate limiter instance
rate_limiter = FitbitRateLimiter()

# Simple in-memory cache to avoid repeated API calls for same data
exercise_cache = {}
health_metrics_cache = {}  # Add cache for health metrics
fitbit_api_cache = {}  # General API cache
CACHE_DURATION_MINUTES = 10  # Cache for 10 minutes

def get_cache_key(access_token, endpoint):
    """Generate a cache key for an API endpoint"""
    # Use last 8 characters of token hash + endpoint for cache key
    token_hash = hashlib.md5(access_token.encode()).hexdigest()[-8:]
    return f"{token_hash}_{endpoint}"

def is_cache_valid(cache_entry):
    """Check if cache entry is still valid"""
    if not cache_entry:
        return False
    return time.time() - cache_entry.get('timestamp', 0) < (CACHE_DURATION_MINUTES * 60)

def get_cached_api_data(access_token, endpoint):
    """Get data from cache if available and valid"""
    cache_key = get_cache_key(access_token, endpoint)
    cache_entry = fitbit_api_cache.get(cache_key)
    
    if is_cache_valid(cache_entry):
        print(f"📦 Using cached data for {endpoint}")
        return cache_entry['data']
    return None

def cache_api_data(access_token, endpoint, data):
    """Cache data for future use"""
    cache_key = get_cache_key(access_token, endpoint)
    fitbit_api_cache[cache_key] = {
        'data': data,
        'timestamp': time.time()
    }
    print(f"💾 Cached data for {endpoint}")

# Health score calculation tracking
health_score_progress = {}  # Track calculation progress by session_id
health_score_results = {}   # Store calculation results by session_id

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

# Check if environment variables are set
if not CLIENT_ID or not CLIENT_SECRET:
    print("⚠️  Environment variables not set!")
    print("Please set your Fitbit API credentials in your .env file")
    print("Check .env.example for required environment variables")
    print("\nMissing variables:")
    if not CLIENT_ID:
        print("  - CLIENT_ID")
    if not CLIENT_SECRET:
        print("  - CLIENT_SECRET")
    exit(1)
REDIRECT_URI = "http://localhost:3000/callback"

@app.route("/")
def index():
    """Home page - shows login or dashboard based on auth status"""
    if 'access_token' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route("/login")

def login():
    auth_url = (
        "https://www.fitbit.com/oauth2/authorize"
        f"?response_type=code&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        "&scope=activity heartrate nutrition profile sleep weight temperature oxygen_saturation respiratory_rate cardio_fitness"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return {"error": "Authorization code not provided"}, 400
        
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    try:
        token_res = requests.post(
            "https://api.fitbit.com/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
                "code": code,
            },
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,  # Add timeout to prevent hanging
        )
        
        if token_res.status_code != 200:
            return {"error": "Failed to obtain access token", "details": token_res.text}, 400

        token_data = token_res.json()
        if "access_token" not in token_data:
            return {"error": "Access token not found in response"}, 400
            
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token")  # Fitbit provides refresh tokens
        expires_in = token_data.get("expires_in", 3600)  # Default to 1 hour if not provided
        
        # Calculate expiration time
        expires_at = time.time() + expires_in
        
        # Store tokens and expiration in session
        session['access_token'] = access_token
        session['refresh_token'] = refresh_token
        session['token_expires_at'] = expires_at
        print(f"✅ Got access token and refresh token, expires in {expires_in} seconds")

        # Fetch user profile
        profile_res = requests.get(
            "https://api.fitbit.com/1/user/-/profile.json",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,  # Add timeout to prevent hanging
        )
        
        if profile_res.status_code != 200:
            return {"error": "Failed to fetch profile", "details": profile_res.text}, 400

        profile_data = profile_res.json()
        
        # Store profile in session
        session['profile'] = profile_data
        
        # Redirect to dashboard instead of returning JSON
        return redirect(url_for('dashboard'))
        
    except requests.exceptions.RequestException as e:
        return {"error": "Network error occurred", "details": str(e)}, 500
    except ValueError as e:
        return {"error": "JSON parsing error", "details": str(e)}, 500
    except Exception as e:
        return {"error": "Unexpected error occurred", "details": str(e)}, 500

def refresh_access_token():
    """Refresh the Fitbit access token using the refresh token"""
    if 'refresh_token' not in session:
        print("❌ No refresh token available, user needs to re-authenticate")
        return False
        
    refresh_token = session['refresh_token']
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    
    try:
        print(f"🔄 Refreshing Fitbit access token...")
        token_res = requests.post(
            "https://api.fitbit.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        
        if token_res.status_code != 200:
            print(f"❌ Token refresh failed: {token_res.status_code} - {token_res.text}")
            return False

        token_data = token_res.json()
        if "access_token" not in token_data:
            print("❌ No access token in refresh response")
            return False
            
        # Update session with new tokens
        session['access_token'] = token_data["access_token"]
        if 'refresh_token' in token_data:  # Sometimes Fitbit provides new refresh token
            session['refresh_token'] = token_data["refresh_token"]
        
        expires_in = token_data.get("expires_in", 3600)
        session['token_expires_at'] = time.time() + expires_in
        
        print(f"✅ Successfully refreshed access token, expires in {expires_in} seconds")
        return True
        
    except Exception as e:
        print(f"❌ Error refreshing token: {e}")
        return False

def get_valid_access_token():
    """Get a valid access token, refreshing if necessary"""
    if 'access_token' not in session:
        return None
        
    # Check if token is expired (refresh 5 minutes before expiry)
    expires_at = session.get('token_expires_at', 0)
    if time.time() > (expires_at - 300):  # 5 minute buffer
        print("🔄 Access token expired or expiring soon, attempting refresh...")
        if not refresh_access_token():
            print("❌ Token refresh failed")
            return None
    
    return session['access_token']

@app.route("/dashboard")
def dashboard():
    """Main dashboard showing health data options"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    profile = session.get('profile', {})
    return render_template('dashboard.html', profile=profile)

@app.route("/logout")
def logout():
    """Clear session and return to home"""
    session.clear()
    return redirect(url_for('index'))

@app.route("/today")
def show_today_activity():
    """Show today's activity data in a user-friendly format"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    access_token = get_valid_access_token()
    if not access_token:
        session.clear()  # Clear invalid session
        return redirect(url_for('index'))
    
    # Get today's activity data
    today_data = get_fitbit_data(access_token, "/1/user/-/activities/date/today.json")
    
    if 'error' in today_data:
        return render_template('activity_data.html', 
                             title="Today's Activity", 
                             error=today_data['error'])
    
    # Extract key metrics
    summary = today_data.get('summary', {})
    
    return render_template('activity_data.html',
                         title="📈 Today's Activity",
                         steps_data=summary.get('steps'),
                         calories_data=summary.get('caloriesOut'),
                         distance_data=round(float(summary.get('distances', [{}])[0].get('distance', 0)), 2) if summary.get('distances') else 0,
                         active_minutes=summary.get('activeScore'))

@app.route("/week")
def show_week_activity():
    """Show past week's activity data"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    access_token = session['access_token']
    
    # Get past 7 days of data
    weekly_data = get_activity_range_data(access_token, 7)
    
    return render_template('activity_data.html',
                         title="📅 Past Week Activity",
                         daily_data=weekly_data)

@app.route("/month")
def show_month_activity():
    """Show past month's activity data"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    access_token = session['access_token']
    
    # Get past 30 days of data
    monthly_data = get_activity_range_data(access_token, 30)
    
    return render_template('activity_data.html',
                         title="📊 Past Month Activity",
                         daily_data=monthly_data)

@app.route("/steps")
def show_steps_history():
    """Show detailed steps history"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    access_token = session['access_token']
    
    # Get date range from query parameter (default to 1 week)
    days = request.args.get('days', 7, type=int)
    
    # Validate days parameter
    valid_ranges = [7, 30, 90, 180]  # 1 week, 1 month, 3 months, 6 months
    if days not in valid_ranges:
        days = 7  # Default to 1 week
    
    # Create title based on selected range
    range_names = {7: "Past Week", 30: "Past Month", 90: "Past 3 Months", 180: "Past 6 Months"}
    title = f"👣 Steps History ({range_names[days]})"
    
    # First, let's test if the access token is valid
    profile_test = get_fitbit_data(access_token, "/1/user/-/profile.json")
    if profile_test and 'error' in profile_test:
        error_msg = profile_test['error']
        # Check if it's a rate limit error
        if 'rate limit' in str(error_msg).lower() or 'too many requests' in str(error_msg).lower():
            error_msg += " Please wait a few minutes before trying again."
        return render_template('activity_data.html',
                             title=title,
                             error=f"Authentication error: {error_msg}",
                             current_days=days)
    
    try:
        # Get activity data for specified number of days
        steps_data = get_activity_range_data(access_token, days, data_type='steps')
        
        # Calculate average steps (excluding days with 'N/A' or 0 values)
        valid_steps = []
        total_steps = 0
        for day in steps_data:
            if day['steps'] != 'N/A' and day['steps'] != '0':
                try:
                    step_count = int(day['steps'])
                    valid_steps.append(step_count)
                    total_steps += step_count
                except (ValueError, TypeError):
                    pass  # Skip invalid values
        
        # Calculate average
        if valid_steps:
            average_steps = round(total_steps / len(valid_steps))
        else:
            average_steps = 0
        
        return render_template('activity_data.html',
                             title=title,
                             daily_data=steps_data,
                             current_days=days,
                             average_steps=average_steps,
                             total_days_with_data=len(valid_steps),
                             total_steps=total_steps)
    
    except Exception as e:
        print(f"Error in steps endpoint: {str(e)}")
        return render_template('activity_data.html',
                             title=title,
                             error=f"An error occurred while retrieving steps data: {str(e)}",
                             current_days=days)

@app.route("/heart-rate")
def show_heart_rate():
    """Show heart rate data with date range selection"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    access_token = session['access_token']
    
    # Get date range from query parameter (default to 1 week)
    days = request.args.get('days', 7, type=int)
    
    # Validate days parameter
    valid_ranges = [7, 30, 90, 180]  # 1 week, 1 month, 3 months, 6 months
    if days not in valid_ranges:
        days = 7  # Default to 1 week
    
    # Create title based on selected range
    range_names = {7: "Past Week", 30: "Past Month", 90: "Past 3 Months", 180: "Past 6 Months"}
    title = f"❤️ Heart Rate Analysis ({range_names[days]})"
    
    # First, let's test if the access token is valid with a simple request
    profile_test = get_fitbit_data(access_token, "/1/user/-/profile.json")
    if profile_test and 'error' in profile_test:
        error_msg = profile_test['error']
        # Check if it's a rate limit error
        if 'rate limit' in str(error_msg).lower() or 'too many requests' in str(error_msg).lower():
            error_msg += " Please wait a few minutes before trying again, or try a smaller date range (1 week instead of 6 months)."
        return render_template('heart_rate.html',
                             title=title,
                             error=f"Authentication error: {error_msg}",
                             current_days=days)
    
    try:
        # Get heart rate data for the specified date range
        heart_rate_data = get_heart_rate_range_data(access_token, days)
        
        # If we have no data, it might be due to rate limiting
        if not heart_rate_data:
            return render_template('heart_rate.html',
                                 title=title,
                                 error="No heart rate data could be retrieved. This might be due to rate limiting. Please try again in a few minutes or select a smaller date range.",
                                 current_days=days)
        
        # Calculate average resting heart rate
        valid_resting_hrs = [day['resting_hr'] for day in heart_rate_data if day['resting_hr'] != 'N/A']
        avg_resting_hr = round(sum(valid_resting_hrs) / len(valid_resting_hrs)) if valid_resting_hrs else 'N/A'
        
        # Calculate average HRV
        valid_hrv_values = [day['hrv_rmssd'] for day in heart_rate_data if day['hrv_rmssd'] != 'N/A']
        avg_hrv = round(sum(valid_hrv_values) / len(valid_hrv_values), 1) if valid_hrv_values else 'N/A'
        
        return render_template('heart_rate.html',
                             title=title,
                             daily_data=heart_rate_data,
                             current_days=days,
                             avg_resting_hr=avg_resting_hr,
                             avg_hrv=avg_hrv,
                             total_days_with_data=len([d for d in heart_rate_data if d['resting_hr'] != 'N/A']))
    
    except Exception as e:
        print(f"Error in heart rate endpoint: {str(e)}")
        return render_template('heart_rate.html',
                             title=title,
                             error=f"An error occurred while retrieving heart rate data: {str(e)}",
                             current_days=days)

@app.route("/sleep")
def show_sleep():
    """Show sleep data with date range options"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    access_token = session['access_token']
    
    # Get number of days from query parameter (default: 7 days = 1 week)
    try:
        days = int(request.args.get('days', 7))
        # Validate days parameter
        if days not in [7, 30, 90, 180]:
            days = 7  # Default to 1 week
    except (ValueError, TypeError):
        days = 7  # Default to 1 week
    
    # Create title based on selected range
    range_names = {7: "Past Week", 30: "Past Month", 90: "Past 3 Months", 180: "Past 6 Months"}
    title = f"😴 Sleep Analysis ({range_names[days]})"
    
    print(f"=== Sleep endpoint called with {days} days ===")
    print(f"Access token exists: {bool(access_token)}")
    
    # First, let's test if the access token is valid
    profile_test = get_fitbit_data(access_token, "/1/user/-/profile.json")
    if profile_test and 'error' in profile_test:
        error_msg = profile_test['error']
        # Check if it's a rate limit error
        if 'rate limit' in str(error_msg).lower() or 'too many requests' in str(error_msg).lower():
            error_msg += " Please wait a few minutes before trying again."
        return render_template('sleep_data.html',
                             title=title,
                             error=f"Authentication error: {error_msg}",
                             current_days=days)
    
    try:
        # Get sleep data for specified number of days using optimized function
        sleep_data = get_sleep_range_data(access_token, days)
        
        # Calculate sleep statistics
        valid_sleep_data = [day for day in sleep_data if day['total_hours'] != 'N/A']
        
        # Calculate averages
        avg_sleep_hours = 0
        avg_efficiency = 0
        avg_deep_minutes = 0
        avg_rem_minutes = 0
        avg_light_minutes = 0
        
        if valid_sleep_data:
            total_hours = sum([day['total_hours'] for day in valid_sleep_data])
            avg_sleep_hours = round(total_hours / len(valid_sleep_data), 1)
            
            # Calculate other averages for days with complete data
            efficiencies = [day['efficiency'] for day in valid_sleep_data if day['efficiency'] != 'N/A']
            if efficiencies:
                avg_efficiency = round(sum(efficiencies) / len(efficiencies))
            
            deep_minutes = [day['deep_minutes'] for day in valid_sleep_data if day['deep_minutes'] != 'N/A']
            if deep_minutes:
                avg_deep_minutes = round(sum(deep_minutes) / len(deep_minutes))
            
            rem_minutes = [day['rem_minutes'] for day in valid_sleep_data if day['rem_minutes'] != 'N/A']
            if rem_minutes:
                avg_rem_minutes = round(sum(rem_minutes) / len(rem_minutes))
            
            light_minutes = [day['light_minutes'] for day in valid_sleep_data if day['light_minutes'] != 'N/A']
            if light_minutes:
                avg_light_minutes = round(sum(light_minutes) / len(light_minutes))
        
        return render_template('sleep_data.html',
                             title=title,
                             sleep_data=sleep_data,
                             current_days=days,
                             avg_sleep_hours=avg_sleep_hours,
                             avg_efficiency=avg_efficiency,
                             avg_deep_minutes=avg_deep_minutes,
                             avg_rem_minutes=avg_rem_minutes,
                             avg_light_minutes=avg_light_minutes,
                             total_days_with_data=len(valid_sleep_data))
    
    except Exception as e:
        print(f"Error in sleep endpoint: {str(e)}")
        return render_template('sleep_data.html',
                             title=title,
                             error=f"Error loading sleep data: {str(e)}",
                             current_days=days)

@app.route("/test-body-api")
@debug_only
def test_body_api():
    """Test body endpoints to debug the issue"""
    if 'access_token' not in session:
        return jsonify({'error': 'Not authenticated', 'redirect_to': '/login'})
    
    access_token = session['access_token']
    today = datetime.now().strftime('%Y-%m-%d')
    
    print(f"=== Testing body API endpoints for {today} ===")
    
    test_results = {
        'date_tested': today,
        'endpoints_tested': {}
    }
    
    # Test the working single-day endpoints first
    endpoints_to_test = [
        f"/1/user/-/body/log/weight/date/{today}.json",
        f"/1/user/-/body/bmi/date/{today}/1d.json",
        f"/1/user/-/body/log/fat/date/{today}.json",
        
        # Test time series formats
        f"/1/user/-/body/weight/date/{today}/{today}.json",
        f"/1/user/-/body/bmi/date/{today}/{today}.json",
        f"/1/user/-/body/fat/date/{today}/{today}.json",
        
        # Test other possible formats
        f"/1/user/-/body/log/weight/date/{today}/{today}.json",
        f"/1/user/-/body/log/fat/date/{today}/{today}.json",
    ]
    
    for endpoint in endpoints_to_test:
        print(f"Testing endpoint: {endpoint}")
        result = get_fitbit_data(access_token, endpoint)
        test_results['endpoints_tested'][endpoint] = result
        
        # Short summary for web display
        if result and not result.get('error'):
            if 'weight' in result and result['weight']:
                test_results['endpoints_tested'][endpoint] = {'status': 'SUCCESS', 'data_found': len(result['weight'])}
            elif 'body-bmi' in result and result['body-bmi']:
                test_results['endpoints_tested'][endpoint] = {'status': 'SUCCESS', 'data_found': len(result['body-bmi'])}
            elif 'fat' in result and result['fat']:
                test_results['endpoints_tested'][endpoint] = {'status': 'SUCCESS', 'data_found': len(result['fat'])}
            else:
                test_results['endpoints_tested'][endpoint] = {'status': 'SUCCESS', 'no_data': True}
        else:
            error_msg = result.get('error', 'Unknown error') if result else 'No response'
            test_results['endpoints_tested'][endpoint] = {'status': 'FAILED', 'error': error_msg}
    
    return jsonify(test_results)

@app.route("/body")
def show_body_metrics():
    """Show body and weight metrics with date range options"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    access_token = session['access_token']
    
    # Get number of days from query parameter (default: 7 days = 1 week)
    try:
        days = int(request.args.get('days', 7))
        # Validate days parameter
        if days not in [7, 30, 90, 180]:
            days = 7  # Default to 1 week
    except (ValueError, TypeError):
        days = 7  # Default to 1 week
    
    # Create title based on selected range
    range_names = {7: "Past Week", 30: "Past Month", 90: "Past 3 Months", 180: "Past 6 Months"}
    title = f"⚖️ Body & Weight Metrics ({range_names[days]})"
    
    print(f"=== Body endpoint called with {days} days ===")
    print(f"Access token exists: {bool(access_token)}")
    
    # First, let's test if the access token is valid
    profile_test = get_fitbit_data(access_token, "/1/user/-/profile.json")
    if profile_test and 'error' in profile_test:
        error_msg = profile_test['error']
        # Check if it's a rate limit error
        if 'rate limit' in str(error_msg).lower() or 'too many requests' in str(error_msg).lower():
            error_msg += " Please wait a few minutes before trying again."
        return render_template('body_metrics.html',
                             title=title,
                             error=f"Authentication error: {error_msg}",
                             current_days=days)
    
    try:
        # Get body data for specified number of days using optimized function
        body_data = get_body_range_data(access_token, days)
        
        # Calculate body statistics
        valid_weight_data = [day for day in body_data if day['weight'] != 'N/A']
        valid_bmi_data = [day for day in body_data if day['bmi'] != 'N/A']
        valid_fat_data = [day for day in body_data if day['body_fat'] != 'N/A']
        
        # Calculate averages
        avg_weight = 0
        avg_bmi = 0
        avg_body_fat = 0
        weight_change = 'N/A'
        
        if valid_weight_data:
            total_weight = sum([day['weight'] for day in valid_weight_data])
            avg_weight = round(total_weight / len(valid_weight_data), 1)
            
            # Calculate weight change (most recent - oldest)
            if len(valid_weight_data) >= 2:
                newest_weight = valid_weight_data[0]['weight']  # Most recent first
                oldest_weight = valid_weight_data[-1]['weight']  # Oldest last
                weight_change = round(newest_weight - oldest_weight, 1)
        
        if valid_bmi_data:
            total_bmi = sum([day['bmi'] for day in valid_bmi_data])
            avg_bmi = round(total_bmi / len(valid_bmi_data), 1)
        
        if valid_fat_data:
            total_fat = sum([day['body_fat'] for day in valid_fat_data])
            avg_body_fat = round(total_fat / len(valid_fat_data), 1)
        
        return render_template('body_metrics.html',
                             title=title,
                             body_data=body_data,
                             current_days=days,
                             avg_weight=avg_weight,
                             avg_bmi=avg_bmi,
                             avg_body_fat=avg_body_fat,
                             weight_change=weight_change,
                             total_days_with_data=len(valid_weight_data))
    
    except Exception as e:
        print(f"Error in body endpoint: {str(e)}")
        return render_template('body_metrics.html',
                             title=title,
                             error=f"Error loading body data: {str(e)}",
                             current_days=days)

def get_weekly_exercise_data(access_token, days=7):
    """Get exercise data for the past week"""
    weekly_data = []
    
    for i in range(days):
        # Calculate date for each day (going backwards from today)
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        print(f"Getting exercise data for {date}")
        
        # Get activities for this specific date
        daily_activities = get_fitbit_data(access_token, f"/1/user/-/activities/date/{date}.json")
        
        if daily_activities and 'activities' in daily_activities:
            for activity in daily_activities['activities']:
                weekly_data.append({
                    'date': date,
                    'name': activity.get('name', 'Unknown Activity'),
                    'duration': activity.get('duration', 0),
                    'calories': activity.get('calories', 0),
                    'distance': activity.get('distance', 0),
                    'steps': activity.get('steps', 0),
                    'startTime': activity.get('startTime', ''),
                    'logType': activity.get('logType', ''),
                    'activityLevel': activity.get('activityLevel', [])
                })
    
    # Sort by date (most recent first)
    weekly_data.sort(key=lambda x: x['date'], reverse=True)
    return weekly_data

def get_exercise_range_data_enhanced(access_token, days):
    """Enhanced exercise data retrieval combining logged activities and active minutes"""
    import time
    
    print(f"🏋️‍♂️ Getting enhanced exercise/activity data for {days} days")
    
    try:
        # Method 1: Get logged activities (manual entries and structured workouts)
        logged_activities = get_exercise_range_data_fallback(access_token, days)
        print(f"🏋️‍♂️ Found {len(logged_activities)} logged activities")
        
        # Method 2: Get activity summaries which include active minutes
        activity_summaries = get_activity_range_data(access_token, days, 'all')
        print(f"🏋️‍♂️ Found {len(activity_summaries) if activity_summaries else 0} activity summary days")
        
        # Combine and enhance the data
        all_activities = logged_activities.copy() if logged_activities else []
        
        # Add activity summary data as "Daily Active Minutes" entries if we have active minutes
        if activity_summaries:
            for day_summary in activity_summaries:
                if isinstance(day_summary, dict):
                    very_active = day_summary.get('very_active_minutes', 0)
                    fairly_active = day_summary.get('fairly_active_minutes', 0)
                    active_minutes = very_active + fairly_active
                    
                    if active_minutes > 15:  # Only include days with meaningful active minutes
                        date_str = day_summary.get('date_str', day_summary.get('date', 'Unknown'))
                        
                        # Check if we already have logged activities for this day
                        existing_dates = [act.get('date_str') for act in all_activities]
                        if date_str not in existing_dates:
                            summary_activity = {
                                'date': day_summary.get('date', 'Unknown'),
                                'date_str': date_str,
                                'name': 'Active Minutes Summary',
                                'duration_minutes': active_minutes,
                                'calories': max(0, day_summary.get('calories_out', 2000) - 1800),  # Rough active calories estimate
                                'distance': day_summary.get('distance', 0),
                                'steps': day_summary.get('steps', 0),
                                'start_time': 'Various',
                                'log_type': 'Automatic',
                                'activity_level': f"Very: {very_active}min, Fairly: {fairly_active}min",
                                'active_duration': active_minutes * 60000  # Convert to milliseconds
                            }
                            all_activities.append(summary_activity)
        
        # Sort by date (most recent first)
        all_activities.sort(key=lambda x: x.get('date_str', ''), reverse=True)
        
        print(f"🏋️‍♂️ Total combined activities: {len(all_activities)}")
        return all_activities
        
    except Exception as e:
        print(f"Error in enhanced exercise data retrieval: {str(e)}")
        import traceback
        traceback.print_exc()
        # Fall back to just logged activities
        return get_exercise_range_data_fallback(access_token, days)

def get_exercise_range_data(access_token, days):
    """Get exercise data for a range of days - tries optimized API first, falls back to individual calls"""
    import time
    
    print(f"� Getting exercise data for {days} days")
    
    # Simple approach: try the individual day method first since we know it works
    # We'll optimize later once we have it working
    return get_exercise_range_data_fallback(access_token, days)

def get_exercise_range_data_fallback(access_token, days):
    """FALLBACK: Get exercise data using individual day calls (old method)"""
    import time
    
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)
        
        print(f"🔄 FALLBACK: Getting exercise data from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        print(f"Using individual day calls as fallback")
        
        daily_exercise_data = []
        
        # Limit the number of individual calls to prevent timeouts
        max_days = min(days, 30)  # Limit to 30 days for individual calls
        print(f"Making individual API calls for {max_days} days (limited for performance)")
        
        for i in range(max_days):
            current_date = end_date - timedelta(days=i)
            date_str = current_date.strftime('%Y-%m-%d')
            
            print(f"Getting activities for {date_str}")
            
            # Get activities for this specific date
            daily_activities = get_fitbit_data(access_token, f"/1/user/-/activities/date/{date_str}.json")
            
            # Check for API errors
            if daily_activities and 'error' in daily_activities:
                print(f"API error for {date_str}: {daily_activities['error']}")
                if 'rate limit' in str(daily_activities['error']).lower():
                    print("Rate limit hit, stopping further requests")
                    break
                continue
            
            # Process activities for this date
            if daily_activities and 'activities' in daily_activities:
                for activity in daily_activities['activities']:
                    # Calculate duration in minutes
                    duration_ms = activity.get('duration', 0)
                    duration_minutes = round(duration_ms / 60000, 1) if duration_ms > 0 else 0
                    
                    # Format start time
                    start_time = activity.get('startTime', '')
                    formatted_start_time = 'N/A'
                    if start_time:
                        try:
                            # Remove timezone info and format nicely
                            if 'T' in start_time:
                                time_part = start_time.split('T')[1].split('.')[0]  # Get HH:MM:SS part
                                formatted_start_time = time_part[:5]  # Just HH:MM
                            else:
                                formatted_start_time = start_time
                        except:
                            formatted_start_time = start_time
                    
                    activity_data = {
                        'date': current_date.strftime('%b %d, %Y'),
                        'date_str': date_str,
                        'name': activity.get('name', 'Unknown Activity'),
                        'duration_minutes': duration_minutes,
                        'calories': activity.get('calories', 0) if activity.get('calories') else 'N/A',
                        'distance': round(activity.get('distance', 0), 2) if activity.get('distance') else 'N/A',
                        'steps': activity.get('steps', 0) if activity.get('steps') else 'N/A',
                        'start_time': formatted_start_time,
                        'log_type': activity.get('logType', 'Manual'),
                        'activity_level': activity.get('activityLevel', []),
                        'active_duration': activity.get('activeDuration', 0)
                    }
                    
                    daily_exercise_data.append(activity_data)
            
            # Rate limiting to prevent API throttling
            time.sleep(0.2)  # 200ms delay between requests
        
        # If no activities found and we hit rate limits, provide helpful message
        if not daily_exercise_data and max_days < days:
            print(f"Limited to {max_days} days due to API rate limiting")
        
        return daily_exercise_data  # Already in reverse chronological order
        
    except Exception as e:
        print(f"Error in fallback exercise range data: {str(e)}")
        import traceback
        traceback.print_exc()
        return []

@app.route("/exercise")
def show_exercise():
    """Show exercise and workout data with date range options - OPTIMIZED with caching"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    access_token = session['access_token']
    
    # Get number of days from query parameter (default: 7 days = 1 week)
    try:
        days = int(request.args.get('days', 7))
        # Validate days parameter
        if days not in [7, 30, 90, 180]:
            days = 7  # Default to 1 week
    except (ValueError, TypeError):
        days = 7  # Default to 1 week
    
    # Create title based on selected range
    range_names = {7: "Past Week", 30: "Past Month", 90: "Past 3 Months", 180: "Past 6 Months"}
    title = f"🏋️‍♂️ Exercise & Activities ({range_names[days]})"
    
    print(f"=== Exercise endpoint called with {days} days ===")
    print(f"Access token exists: {bool(access_token)}")
    
    # Check cache first to avoid repeated API calls
    cache_key = f"{access_token[-10:]}_{days}_{datetime.now().strftime('%Y-%m-%d_%H')}"  # Cache by hour
    current_time = datetime.now()
    
    if cache_key in exercise_cache:
        cached_data, cached_time = exercise_cache[cache_key]
        if (current_time - cached_time).total_seconds() < CACHE_DURATION_MINUTES * 60:
            print(f"🎯 CACHE HIT: Using cached data from {cached_time.strftime('%H:%M')}")
            return render_template('exercise_data.html', **cached_data)
    
    # First, let's test if the access token is valid
    profile_test = get_fitbit_data(access_token, "/1/user/-/profile.json")
    if profile_test and 'error' in profile_test:
        error_msg = profile_test['error']
        # Check if it's a rate limit error
        if 'rate limit' in str(error_msg).lower() or 'too many requests' in str(error_msg).lower():
            error_msg += " Please wait a few minutes before trying again."
        return render_template('exercise_data.html',
                             title=title,
                             error=f"Authentication error: {error_msg}",
                             current_days=days)
    
    try:
        start_time = datetime.now()
        print(f"⏱️ Starting API calls at {start_time.strftime('%H:%M:%S')}")
        
        # Get exercise data for specified number of days using optimized function
        exercise_data = get_exercise_range_data(access_token, days)
        
        # Also get lifetime stats for context
        lifetime_stats = get_fitbit_data(access_token, "/1/user/-/activities.json")
        
        end_time = datetime.now()
        total_time = (end_time - start_time).total_seconds()
        print(f"⏱️ API calls completed at {end_time.strftime('%H:%M:%S')} - Total time: {total_time:.2f} seconds")
        
        # Calculate exercise statistics
        total_activities = len(exercise_data)
        total_calories = sum([activity['calories'] for activity in exercise_data if activity['calories'] != 'N/A'])
        total_distance = sum([activity['distance'] for activity in exercise_data if activity['distance'] != 'N/A'])
        total_duration = sum([activity['duration_minutes'] for activity in exercise_data if activity['duration_minutes'] != 'N/A'])
        
        # Count activities by type
        activity_types = {}
        for activity in exercise_data:
            activity_type = activity['name']
            if activity_type in activity_types:
                activity_types[activity_type] += 1
            else:
                activity_types[activity_type] = 1
        
        # Get most common activity
        most_common_activity = max(activity_types.items(), key=lambda x: x[1])[0] if activity_types else 'N/A'
        
        # Prepare template data
        template_data = {
            'title': title,
            'exercise_data': exercise_data,
            'lifetime_stats': lifetime_stats,
            'current_days': days,
            'total_activities': total_activities,
            'total_calories': total_calories,
            'total_distance': round(total_distance, 1) if total_distance else 0,
            'total_duration': round(total_duration) if total_duration else 0,
            'most_common_activity': most_common_activity,
            'activity_types': activity_types
        }
        
        # Cache the results for faster subsequent requests
        exercise_cache[cache_key] = (template_data, current_time)
        print(f"💾 CACHED: Data cached at {current_time.strftime('%H:%M')} for {CACHE_DURATION_MINUTES} minutes")
        
        # Clean old cache entries (simple cleanup)
        if len(exercise_cache) > 20:  # Keep cache size reasonable
            oldest_key = min(exercise_cache.keys(), 
                           key=lambda k: exercise_cache[k][1] if len(exercise_cache[k]) > 1 else datetime.min)
            del exercise_cache[oldest_key]
            print("🧹 Cleaned old cache entry")
        
        return render_template('exercise_data.html', **template_data)
    
    except Exception as e:
        print(f"Error in exercise endpoint: {str(e)}")
        return render_template('exercise_data.html',
                             title=title,
                             error=f"Error loading exercise data: {str(e)}",
                             current_days=days)

@app.route("/health-metrics")
def show_health_metrics():
    """Show advanced health metrics with date range support"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    # Get date range parameter, default to 7 days (1 week)
    days = request.args.get('days', '7')
    try:
        days = int(days)
        # Limit to reasonable ranges
        if days not in [7, 30, 90, 180]:
            days = 7
    except ValueError:
        days = 7
    
    # Check cache first
    cache_key = f"health_metrics_{days}"
    if cache_key in health_metrics_cache:
        cached_data, cached_time = health_metrics_cache[cache_key]
        # Use cache if less than 30 minutes old
        if time.time() - cached_time < 1800:  # 30 minutes
            print(f"Using cached health metrics data for {days} days")
            return render_template("health_metrics.html", 
                                 health_data=cached_data, 
                                 selected_days=days,
                                 days_options=[
                                     {'value': 7, 'label': '1 Week'},
                                     {'value': 30, 'label': '1 Month'},
                                     {'value': 90, 'label': '3 Months'},
                                     {'value': 180, 'label': '6 Months'}
                                 ])
    
    try:
        access_token = session['access_token']
        
        # Get health metrics data for the specified range using optimized bulk endpoints
        try:
            health_data = get_health_metrics_range_data_optimized_v2(access_token, days)
            print(f"✅ Bulk endpoint result: {len(health_data)} days retrieved")
        except Exception as e:
            print(f"❌ Bulk endpoints failed: {str(e)}, falling back to individual calls")
            # Fallback to same function but with error handling
            health_data = get_health_metrics_range_data_optimized_v2(access_token, days)
        
        # Calculate summary statistics
        summary = {
            'total_days': len(health_data),
            'days_with_spo2': 0,
            'days_with_breathing_rate': 0,
            'days_with_temp_data': 0,
            'days_with_cardio': 0,
            'avg_spo2': 0,
            'avg_breathing_rate': 0,
            'avg_cardio_fitness': 0
        }
        
        spo2_values = []
        breathing_values = []
        cardio_values = []
        
        for day in health_data:
            if day['spo2'] != 'N/A':
                summary['days_with_spo2'] += 1
                try:
                    spo2_values.append(float(day['spo2']))
                except (ValueError, TypeError):
                    pass
            
            if day['breathing_rate'] != 'N/A':
                summary['days_with_breathing_rate'] += 1
                try:
                    breathing_values.append(float(day['breathing_rate']))
                except (ValueError, TypeError):
                    pass
                    
            if day['temperature_variation'] != 'N/A':
                summary['days_with_temp_data'] += 1
                
            if day['cardio_fitness'] != 'N/A':
                summary['days_with_cardio'] += 1
                try:
                    cardio_values.append(float(day['cardio_fitness']))
                except (ValueError, TypeError):
                    pass
        
        # Calculate averages
        if spo2_values:
            summary['avg_spo2'] = round(sum(spo2_values) / len(spo2_values), 1)
        
        if breathing_values:
            summary['avg_breathing_rate'] = round(sum(breathing_values) / len(breathing_values), 1)
            
        if cardio_values:
            summary['avg_cardio_fitness'] = round(sum(cardio_values) / len(cardio_values), 1)
        
        # Cache the result
        health_metrics_cache[cache_key] = (health_data, time.time())
        
        print(f"Health metrics summary for {days} days: {summary}")
        
        return render_template("health_metrics.html", 
                             health_data=health_data,
                             summary=summary,
                             selected_days=days,
                             days_options=[
                                 {'value': 7, 'label': '1 Week'},
                                 {'value': 30, 'label': '1 Month'},
                                 {'value': 90, 'label': '3 Months'},
                                 {'value': 180, 'label': '6 Months'}
                             ])
    except Exception as e:
        print(f"Error in show_health_metrics: {str(e)}")
        import traceback
        traceback.print_exc()
        return render_template("health_metrics.html", 
                             health_data=[],
                             summary={},
                             selected_days=days,
                             days_options=[
                                 {'value': 7, 'label': '1 Week'},
                                 {'value': 30, 'label': '1 Month'},
                                 {'value': 90, 'label': '3 Months'},
                                 {'value': 180, 'label': '6 Months'}
                             ])

@app.route("/active-zone-minutes")
def show_azm_detailed():
    """Show detailed Active Zone Minutes"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    access_token = session['access_token']
    azm_data = get_active_zone_minutes_detailed(access_token)
    
    return render_template('azm_detailed.html', azm_data=azm_data)

@app.route("/test-api")
@debug_only
def test_api():
    """Test endpoint to see raw API responses"""
    print("\n=== TEST API ENDPOINT CALLED ===")
    if 'access_token' not in session:
        print("ERROR: No access token in session")
        return jsonify({
            'error': 'Not authenticated',
            'session_keys': list(session.keys()),
            'redirect_needed': True
        })
    
    access_token = session['access_token']
    print(f"Access token: {access_token[:20]}...")
    
    # Test today's data
    today = datetime.now().strftime('%Y-%m-%d')
    print(f"Testing data for date: {today}")
    
    # Test profile first to check token validity
    profile_data = get_fitbit_data(access_token, "/1/user/-/profile.json")
    print(f"Profile test result: {profile_data}")
    
    test_data = {
        'date_tested': today,
        'access_token_preview': access_token[:20] + '...',
        'profile': profile_data,
        'steps': get_fitbit_data(access_token, f"/1/user/-/activities/steps/date/{today}/1d.json"),
        'calories': get_fitbit_data(access_token, f"/1/user/-/activities/calories/date/{today}/1d.json"),
        'distance': get_fitbit_data(access_token, f"/1/user/-/activities/distance/date/{today}/1d.json"),
        'today_summary': get_fitbit_data(access_token, "/1/user/-/activities/date/today.json")
    }
    
    print(f"Test data compiled: {test_data}")
    return jsonify(test_data)

def get_activity_range_data(access_token, days, data_type='all'):
    """Helper function to get activity data for a range of days"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days-1)
    
    date_range = f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"
    
    daily_data = []
    
    # Initialize dictionaries
    steps_dict = {}
    calories_dict = {}
    distance_dict = {}
    
    if data_type == 'all' or data_type == 'steps':
        # Get steps data
        steps_response = get_fitbit_data(access_token, f"/1/user/-/activities/steps/date/{date_range}.json")
        print(f"Steps API response: {steps_response}")  # Debug line
        if 'activities-steps' in steps_response:
            for item in steps_response['activities-steps']:
                steps_dict[item['dateTime']] = item['value']
    
    if data_type == 'all':
        # Get calories data
        calories_response = get_fitbit_data(access_token, f"/1/user/-/activities/calories/date/{date_range}.json")
        print(f"Calories API response: {calories_response}")  # Debug line
        if 'activities-calories' in calories_response:
            for item in calories_response['activities-calories']:
                calories_dict[item['dateTime']] = item['value']
        
        # Get distance data
        distance_response = get_fitbit_data(access_token, f"/1/user/-/activities/distance/date/{date_range}.json")
        print(f"Distance API response: {distance_response}")  # Debug line
        if 'activities-distance' in distance_response:
            for item in distance_response['activities-distance']:
                distance_dict[item['dateTime']] = round(float(item['value']), 2)
    
    # For steps-only view, we still want to show calories and distance if available
    elif data_type == 'steps':
        # Also get calories and distance for steps history
        calories_response = get_fitbit_data(access_token, f"/1/user/-/activities/calories/date/{date_range}.json")
        if 'activities-calories' in calories_response:
            for item in calories_response['activities-calories']:
                calories_dict[item['dateTime']] = item['value']
        
        distance_response = get_fitbit_data(access_token, f"/1/user/-/activities/distance/date/{date_range}.json")
        if 'activities-distance' in distance_response:
            for item in distance_response['activities-distance']:
                distance_dict[item['dateTime']] = round(float(item['value']), 2)
    
    # Combine data by date
    current_date = start_date
    while current_date <= end_date:
        date_str = current_date.strftime('%Y-%m-%d')
        day_data = {
            'date': current_date.strftime('%b %d, %Y'),
            'steps': steps_dict.get(date_str, 'N/A'),
            'calories': calories_dict.get(date_str, 'N/A'),
            'distance': distance_dict.get(date_str, 'N/A')
        }
        
        daily_data.append(day_data)
        current_date += timedelta(days=1)
    
    return list(reversed(daily_data))  # Most recent first

def get_heart_rate_data(access_token, date="today"):
    """Get heart rate data including resting HR, zones, and intraday data"""
    heart_data = {}
    
    # Get daily heart rate summary
    hr_summary = get_fitbit_data(access_token, f"/1/user/-/activities/heart/date/{date}/1d.json")
    heart_data['summary'] = hr_summary
    
    # Get intraday heart rate (1-minute intervals)
    intraday_hr = get_fitbit_data(access_token, f"/1/user/-/activities/heart/date/{date}/1d/1min.json")
    heart_data['intraday'] = intraday_hr
    
    # Get HRV data
    hrv_data = get_fitbit_data(access_token, f"/1/user/-/hrv/date/{date}.json")
    heart_data['hrv'] = hrv_data
    
    return heart_data

def get_heart_rate_range_data(access_token, days):
    """Get heart rate data for a range of days using efficient time series API"""
    import time
    
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)
        
        # Format dates for API call
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        
        print(f"Getting heart rate time series from {start_str} to {end_str}")
        
        # Use time series endpoint for heart rate data (much more efficient!)
        # This gets all heart rate data in one API call instead of individual day calls
        time_series_url = f"/1/user/-/activities/heart/date/{start_str}/{end_str}.json"
        hr_time_series = get_fitbit_data(access_token, time_series_url)
        
        if hr_time_series and 'error' in hr_time_series:
            print(f"Error getting heart rate time series: {hr_time_series['error']}")
            return []
        
        daily_hr_data = []
        
        # Process the time series data
        if hr_time_series and 'activities-heart' in hr_time_series:
            for hr_day in hr_time_series['activities-heart']:
                date_str = hr_day['dateTime']
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                
                day_data = {
                    'date': date_obj.strftime('%b %d, %Y'),
                    'date_str': date_str,
                    'resting_hr': 'N/A',
                    'hrv_rmssd': 'N/A',
                    'hrv_deep': 'N/A',
                    'zones': {},
                    'total_zone_minutes': 0
                }
                
                # Extract heart rate data
                if hr_day.get('value'):
                    if hr_day['value'].get('restingHeartRate'):
                        day_data['resting_hr'] = hr_day['value']['restingHeartRate']
                    
                    # Extract heart rate zones
                    if hr_day['value'].get('heartRateZones'):
                        total_minutes = 0
                        for zone in hr_day['value']['heartRateZones']:
                            zone_name = zone.get('name', 'Unknown')
                            minutes = zone.get('minutes', 0)
                            day_data['zones'][zone_name] = minutes
                            total_minutes += minutes
                        day_data['total_zone_minutes'] = total_minutes
                
                daily_hr_data.append(day_data)
        
        # For HRV data, we still need individual calls since there's no time series endpoint
        # But we'll limit this to prevent timeout issues
        print("Getting HRV data (limited to prevent timeouts)...")
        
        # Only get HRV for recent days to avoid too many API calls
        hrv_days_limit = min(days, 14)  # Limit HRV to last 14 days max
        hrv_data_to_fetch = daily_hr_data[-hrv_days_limit:] if len(daily_hr_data) > hrv_days_limit else daily_hr_data
        
        for i, day_data in enumerate(hrv_data_to_fetch):
            # Small delay to prevent rate limiting
            time.sleep(0.3)  
            
            hrv_data = get_fitbit_data(access_token, f"/1/user/-/hrv/date/{day_data['date_str']}.json")
            
            # Check for rate limit error on HRV
            if hrv_data and 'error' in hrv_data:
                print(f"Error getting HRV data for {day_data['date_str']}: {hrv_data['error']}")
                if 'rate limit' in str(hrv_data['error']).lower():
                    print("Rate limit hit on HRV data, skipping remaining HRV requests")
                    break
            
            # Process HRV data
            if hrv_data and 'hrv' in hrv_data and hrv_data['hrv']:
                hrv_entry = hrv_data['hrv'][0] if hrv_data['hrv'] else None
                if hrv_entry and hrv_entry.get('value'):
                    # Find the matching day in our full dataset
                    matching_day_index = len(daily_hr_data) - hrv_days_limit + i
                    if 0 <= matching_day_index < len(daily_hr_data):
                        if hrv_entry['value'].get('dailyRmssd'):
                            daily_hr_data[matching_day_index]['hrv_rmssd'] = hrv_entry['value']['dailyRmssd']
                        if hrv_entry['value'].get('deepRmssd'):
                            daily_hr_data[matching_day_index]['hrv_deep'] = hrv_entry['value']['deepRmssd']
        
        return list(reversed(daily_hr_data))  # Most recent first
        
    except Exception as e:
        print(f"Error getting heart rate range data: {str(e)}")
        return []

def get_sleep_range_data(access_token, days):
    """Get sleep data for a range of days using efficient time series API"""
    import time
    
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)
        
        # Format dates for API call
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        
        print(f"Getting sleep time series from {start_str} to {end_str}")
        
        # Use time series endpoint for sleep data (much more efficient!)
        # This gets all sleep data in one API call instead of individual day calls
        time_series_url = f"/1.2/user/-/sleep/date/{start_str}/{end_str}.json"
        sleep_time_series = get_fitbit_data(access_token, time_series_url)
        
        if sleep_time_series and 'error' in sleep_time_series:
            print(f"Error getting sleep time series: {sleep_time_series['error']}")
            return []
        
        daily_sleep_data = []
        
        # Process the time series data
        if sleep_time_series and 'sleep' in sleep_time_series:
            # Create a date-indexed dictionary for efficient lookup
            sleep_by_date = {}
            
            for sleep_entry in sleep_time_series['sleep']:
                # Get the date from the sleep entry
                date_str = sleep_entry['dateOfSleep']
                
                if date_str not in sleep_by_date:
                    sleep_by_date[date_str] = []
                sleep_by_date[date_str].append(sleep_entry)
            
            # Create daily data for each date in range
            current_date = start_date
            while current_date <= end_date:
                date_str = current_date.strftime('%Y-%m-%d')
                date_obj = current_date
                
                day_data = {
                    'date': date_obj.strftime('%b %d, %Y'),
                    'date_str': date_str,
                    'total_hours': 'N/A',
                    'efficiency': 'N/A',
                    'minutes_to_fall_asleep': 'N/A',
                    'minutes_awake': 'N/A',
                    'wake_minutes': 'N/A',
                    'light_minutes': 'N/A',
                    'deep_minutes': 'N/A',
                    'rem_minutes': 'N/A',
                    'sleep_start': 'N/A',
                    'sleep_end': 'N/A',
                    'restless_count': 'N/A',
                    'sleep_score': 'N/A'
                }
                
                # Check if we have sleep data for this date
                if date_str in sleep_by_date:
                    sleep_entries = sleep_by_date[date_str]
                    # Use the main sleep entry (usually the longest one)
                    main_sleep = max(sleep_entries, key=lambda x: x.get('duration', 0))
                    
                    # Extract sleep metrics
                    if main_sleep.get('duration'):
                        day_data['total_hours'] = round(main_sleep['duration'] / 60000 / 60, 1)
                    
                    if main_sleep.get('efficiency'):
                        day_data['efficiency'] = main_sleep['efficiency']
                    
                    if main_sleep.get('minutesToFallAsleep'):
                        day_data['minutes_to_fall_asleep'] = main_sleep['minutesToFallAsleep']
                    
                    if main_sleep.get('minutesAwake'):
                        day_data['minutes_awake'] = main_sleep['minutesAwake']
                    
                    if main_sleep.get('startTime'):
                        day_data['sleep_start'] = main_sleep['startTime']
                    
                    if main_sleep.get('endTime'):
                        day_data['sleep_end'] = main_sleep['endTime']
                    
                    if main_sleep.get('restlessCount'):
                        day_data['restless_count'] = main_sleep['restlessCount']
                    
                    # Extract sleep stage data
                    if main_sleep.get('levels') and main_sleep['levels'].get('summary'):
                        summary = main_sleep['levels']['summary']
                        
                        if summary.get('wake') and summary['wake'].get('minutes'):
                            day_data['wake_minutes'] = summary['wake']['minutes']
                        
                        if summary.get('light') and summary['light'].get('minutes'):
                            day_data['light_minutes'] = summary['light']['minutes']
                        
                        if summary.get('deep') and summary['deep'].get('minutes'):
                            day_data['deep_minutes'] = summary['deep']['minutes']
                        
                        if summary.get('rem') and summary['rem'].get('minutes'):
                            day_data['rem_minutes'] = summary['rem']['minutes']
                
                daily_sleep_data.append(day_data)
                current_date += timedelta(days=1)
        
        return list(reversed(daily_sleep_data))  # Most recent first
        
    except Exception as e:
        print(f"Error getting sleep range data: {str(e)}")
        return []

def get_body_range_data(access_token, days):
    """Get body data for a range of days using the working Fitbit API endpoints"""
    import time
    
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)
        
        print(f"Getting body data from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        
        # Based on Fitbit API documentation, the correct time series endpoints are:
        # Weight: /1/user/-/body/weight/date/{start-date}/{end-date}.json
        # BMI: /1/user/-/body/bmi/date/{start-date}/{end-date}.json  
        # Body Fat: /1/user/-/body/fat/date/{start-date}/{end-date}.json
        
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        
        print(f"Trying correct Fitbit API time series endpoints...")
        
        # Get weight time series data
        print("Getting weight data...")
        weight_response = get_fitbit_data(access_token, f"/1/user/-/body/weight/date/{start_str}/{end_str}.json")
        print(f"Weight time series response: {weight_response}")
        
        # Get BMI time series data
        print("Getting BMI data...")
        bmi_response = get_fitbit_data(access_token, f"/1/user/-/body/bmi/date/{start_str}/{end_str}.json")
        print(f"BMI time series response: {bmi_response}")
        
        # Get body fat time series data  
        print("Getting body fat data...")
        fat_response = get_fitbit_data(access_token, f"/1/user/-/body/fat/date/{start_str}/{end_str}.json")
        print(f"Body fat time series response: {fat_response}")
        
        daily_body_data = []
        
        # Create a date-indexed dictionary for efficient lookup
        weight_by_date = {}
        bmi_by_date = {}
        fat_by_date = {}
        
        # Process weight data - correct response structure
        if weight_response and 'body-weight' in weight_response and not weight_response.get('error'):
            print(f"Processing weight data: {len(weight_response['body-weight'])} entries")
            for entry in weight_response['body-weight']:
                weight_by_date[entry['dateTime']] = entry['value']
        
        # Process BMI data - correct response structure  
        if bmi_response and 'body-bmi' in bmi_response and not bmi_response.get('error'):
            print(f"Processing BMI data: {len(bmi_response['body-bmi'])} entries")
            for entry in bmi_response['body-bmi']:
                bmi_by_date[entry['dateTime']] = entry['value']
        
        # Process body fat data - correct response structure
        if fat_response and 'body-fat' in fat_response and not fat_response.get('error'):
            print(f"Processing body fat data: {len(fat_response['body-fat'])} entries")
            for entry in fat_response['body-fat']:
                fat_by_date[entry['dateTime']] = entry['value']
        
        # If time series didn't work, fall back to individual day calls for recent days
        if not weight_by_date and not bmi_by_date and not fat_by_date:
            print("Time series failed, using individual day calls...")
            return get_body_individual_days(access_token, days)
        
        # Create daily data for each date in range
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime('%Y-%m-%d')
            date_obj = current_date
            
            day_data = {
                'date': date_obj.strftime('%b %d, %Y'),
                'date_str': date_str,
                'weight': 'N/A',
                'bmi': 'N/A',
                'body_fat': 'N/A',
                'weight_unit': 'kg',
                'bmi_category': 'N/A'
            }
            
            # Add weight data if available
            if date_str in weight_by_date:
                day_data['weight'] = round(float(weight_by_date[date_str]), 1)
            
            # Add BMI data if available
            if date_str in bmi_by_date:
                bmi_value = round(float(bmi_by_date[date_str]), 1)
                day_data['bmi'] = bmi_value
                
                # Calculate BMI category
                if bmi_value < 18.5:
                    day_data['bmi_category'] = 'Underweight'
                elif bmi_value < 25:
                    day_data['bmi_category'] = 'Normal'
                elif bmi_value < 30:
                    day_data['bmi_category'] = 'Overweight'
                else:
                    day_data['bmi_category'] = 'Obese'
            
            # Add body fat data if available
            if date_str in fat_by_date:
                day_data['body_fat'] = round(float(fat_by_date[date_str]), 1)
            
            daily_body_data.append(day_data)
            current_date += timedelta(days=1)
        
        print(f"Returning {len(daily_body_data)} days of body data")
        return list(reversed(daily_body_data))  # Most recent first
        
    except Exception as e:
        print(f"Error getting body range data: {str(e)}")
        import traceback
        traceback.print_exc()
        # Fall back to individual day calls
        return get_body_individual_days(access_token, days)

def get_body_individual_days(access_token, days):
    """Fallback method: Get body data using individual day API calls"""
    import time
    
    print(f"Using individual day calls for body data (max {min(days, 7)} days)")
    
    daily_body_data = []
    max_days = min(days, 7)  # Limit to 7 days to prevent timeouts
    
    for i in range(max_days):
        current_date = datetime.now() - timedelta(days=i)
        date_str = current_date.strftime('%Y-%m-%d')
        
        print(f"Getting body data for {date_str} (individual call)")
        
        day_data = {
            'date': current_date.strftime('%b %d, %Y'),
            'date_str': date_str,
            'weight': 'N/A',
            'bmi': 'N/A',
            'body_fat': 'N/A',
            'weight_unit': 'kg',
            'bmi_category': 'N/A'
        }
        
        # Get weight data for this day
        weight_response = get_fitbit_data(access_token, f"/1/user/-/body/log/weight/date/{date_str}.json")
        if weight_response and 'weight' in weight_response and weight_response['weight']:
            latest_entry = weight_response['weight'][-1]  # Most recent entry
            if 'weight' in latest_entry:
                day_data['weight'] = round(float(latest_entry['weight']), 1)
            if 'bmi' in latest_entry:
                bmi_val = round(float(latest_entry['bmi']), 1)
                day_data['bmi'] = bmi_val
                # Calculate BMI category
                if bmi_val < 18.5:
                    day_data['bmi_category'] = 'Underweight'
                elif bmi_val < 25:
                    day_data['bmi_category'] = 'Normal'
                elif bmi_val < 30:
                    day_data['bmi_category'] = 'Overweight'
                else:
                    day_data['bmi_category'] = 'Obese'
            if 'fat' in latest_entry:
                day_data['body_fat'] = round(float(latest_entry['fat']), 1)
        
        # Get BMI data if not found in weight
        if day_data['bmi'] == 'N/A':
            bmi_response = get_fitbit_data(access_token, f"/1/user/-/body/bmi/date/{date_str}/1d.json")
            if bmi_response and 'body-bmi' in bmi_response and bmi_response['body-bmi']:
                bmi_val = round(float(bmi_response['body-bmi'][0]['value']), 1)
                day_data['bmi'] = bmi_val
                # Calculate BMI category
                if bmi_val < 18.5:
                    day_data['bmi_category'] = 'Underweight'
                elif bmi_val < 25:
                    day_data['bmi_category'] = 'Normal'
                elif bmi_val < 30:
                    day_data['bmi_category'] = 'Overweight'
                else:
                    day_data['bmi_category'] = 'Obese'
        
        # Get body fat data if not found in weight
        if day_data['body_fat'] == 'N/A':
            fat_response = get_fitbit_data(access_token, f"/1/user/-/body/log/fat/date/{date_str}.json")
            if fat_response and 'fat' in fat_response and fat_response['fat']:
                day_data['body_fat'] = round(float(fat_response['fat'][-1]['fat']), 1)
        
        daily_body_data.append(day_data)
        time.sleep(0.3)  # Rate limiting
    
    return daily_body_data  # Already in reverse chronological order

def get_sleep_data(access_token, date=None):
    """Get comprehensive sleep data"""
    sleep_data = {}
    
    # Convert date to proper format
    if date is None or date == "today":
        date = datetime.now().strftime('%Y-%m-%d')
    
    print(f"=== Getting sleep data for date: {date} ===")
    
    # Get sleep log for specific date
    sleep_log = get_fitbit_data(access_token, f"/1.2/user/-/sleep/date/{date}.json")
    print(f"Sleep log response: {sleep_log}")
    sleep_data['daily'] = sleep_log
    
    # Get sleep efficiency data (note: this endpoint might not exist, let's remove it for now)
    # sleep_efficiency = get_fitbit_data(access_token, f"/1/user/-/sleep/efficiency/date/{date}/1d.json")
    # print(f"Sleep efficiency response: {sleep_efficiency}")
    # sleep_data['efficiency'] = sleep_efficiency
    
    return sleep_data

def get_body_data(access_token, date=None):
    """Get body and weight data"""
    body_data = {}
    
    # Convert date to proper format
    if date is None or date == "today":
        date = datetime.now().strftime('%Y-%m-%d')
    
    # Weight data
    weight_data = get_fitbit_data(access_token, f"/1/user/-/body/log/weight/date/{date}.json")
    body_data['weight'] = weight_data
    
    # BMI data
    bmi_data = get_fitbit_data(access_token, f"/1/user/-/body/bmi/date/{date}/1d.json")
    body_data['bmi'] = bmi_data
    
    # Body fat data
    fat_data = get_fitbit_data(access_token, f"/1/user/-/body/log/fat/date/{date}.json")
    body_data['fat'] = fat_data
    
    return body_data

def get_exercise_data(access_token, date=None):
    """Get exercise and workout data"""
    exercise_data = {}
    
    # Convert date to proper format
    if date is None or date == "today":
        date = datetime.now().strftime('%Y-%m-%d')
    
    # Get activities/exercises for the date
    activities = get_fitbit_data(access_token, f"/1/user/-/activities/date/{date}.json")
    exercise_data['activities'] = activities
    
    # Get lifetime stats
    lifetime_stats = get_fitbit_data(access_token, "/1/user/-/activities.json")
    exercise_data['lifetime'] = lifetime_stats
    
    return exercise_data

def get_health_metrics_range_data_optimized_v2(access_token, days):
    """Get health metrics data with rate limit optimization"""
    import time
    
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)
        
        print(f"Getting health metrics data from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        
        daily_health_data = []
        rate_limit_errors = 0
        max_rate_limit_errors = 5  # Stop if we hit too many rate limits
        
        # Process each day individually to ensure data is fetched correctly
        for i in range(days):
            current_date = end_date - timedelta(days=i)
            date_str = current_date.strftime('%Y-%m-%d')
            
            print(f"Processing health metrics for {date_str} ({i+1}/{days})")
            
            # If we've hit too many rate limits, use minimal data
            if rate_limit_errors >= max_rate_limit_errors:
                print(f"⚠️  Too many rate limit errors ({rate_limit_errors}), using minimal data for remaining days")
                day_data = {
                    'date': current_date.strftime('%b %d, %Y'),
                    'date_str': date_str,
                    'spo2': 'N/A',
                    'breathing_rate': 'N/A', 
                    'temperature_variation': 'N/A',
                    'cardio_fitness': 'N/A'
                }
                daily_health_data.append(day_data)
                continue
            
            day_data = {
                'date': current_date.strftime('%b %d, %Y'),
                'date_str': date_str,
                'spo2': 'N/A',
                'breathing_rate': 'N/A',
                'temperature_variation': 'N/A',
                'cardio_fitness': 'N/A'
            }
            
            # SpO2 data - use the correct API endpoint
            print(f"  Fetching SpO2 data for {date_str}...")
            spo2_data = get_fitbit_data(access_token, f"/1/user/-/spo2/date/{date_str}.json")
            print(f"  SpO2 response: {spo2_data}")
            
            if spo2_data and spo2_data.get('status') == 429:
                rate_limit_errors += 1
                print(f"⚠️  Rate limit hit on SpO2 call (error #{rate_limit_errors})")
            elif spo2_data and not spo2_data.get('error'):
                if 'value' in spo2_data:
                    spo2_value = spo2_data['value']
                    if isinstance(spo2_value, list) and len(spo2_value) > 0:
                        # Take the first reading from the list
                        first_reading = spo2_value[0]
                        if isinstance(first_reading, dict):
                            day_data['spo2'] = first_reading.get('avg', first_reading.get('value', 'N/A'))
                        else:
                            day_data['spo2'] = first_reading
                    elif isinstance(spo2_value, dict):
                        day_data['spo2'] = spo2_value.get('avg', spo2_value.get('value', 'N/A'))
                    elif isinstance(spo2_value, (int, float)):
                        day_data['spo2'] = spo2_value
                elif isinstance(spo2_data, list) and len(spo2_data) > 0:
                    # Handle case where data is directly a list
                    first_item = spo2_data[0]
                    if isinstance(first_item, dict):
                        day_data['spo2'] = first_item.get('avg', first_item.get('value', 'N/A'))
            
            # Breathing rate data
            print(f"  Fetching breathing rate data for {date_str}...")
            breathing_data = get_fitbit_data(access_token, f"/1/user/-/br/date/{date_str}.json")
            print(f"  Breathing response: {breathing_data}")
            
            if breathing_data and breathing_data.get('status') == 429:
                rate_limit_errors += 1
                print(f"⚠️  Rate limit hit on breathing rate call (error #{rate_limit_errors})")
            elif breathing_data and not breathing_data.get('error'):
                if 'br' in breathing_data:
                    br_readings = breathing_data['br']
                    if isinstance(br_readings, list) and len(br_readings) > 0:
                        first_br = br_readings[0]
                        if isinstance(first_br, dict) and 'value' in first_br:
                            breathing_value = first_br['value']
                            if isinstance(breathing_value, dict):
                                day_data['breathing_rate'] = breathing_value.get('breathingRate', breathing_value.get('value', 'N/A'))
                            else:
                                day_data['breathing_rate'] = breathing_value
                elif 'value' in breathing_data:
                    # Handle alternate structure
                    breathing_value = breathing_data['value']
                    if isinstance(breathing_value, dict):
                        day_data['breathing_rate'] = breathing_value.get('breathingRate', breathing_value.get('value', 'N/A'))
                    else:
                        day_data['breathing_rate'] = breathing_value
            
            # VO2 Max / Cardio Fitness data
            print(f"  Fetching cardio fitness data for {date_str}...")
            cardio_data = get_fitbit_data(access_token, f"/1/user/-/cardioscore/date/{date_str}.json")
            print(f"  Cardio response: {cardio_data}")
            
            if cardio_data and cardio_data.get('status') == 429:
                rate_limit_errors += 1
                print(f"⚠️  Rate limit hit on cardio fitness call (error #{rate_limit_errors})")
            elif cardio_data and not cardio_data.get('error'):
                if 'cardioScore' in cardio_data:
                    cardio_readings = cardio_data['cardioScore']
                    if isinstance(cardio_readings, list) and len(cardio_readings) > 0:
                        first_cardio = cardio_readings[0]
                        if isinstance(first_cardio, dict) and 'value' in first_cardio:
                            cardio_value = first_cardio['value']
                            if isinstance(cardio_value, dict):
                                day_data['cardio_fitness'] = cardio_value.get('vo2Max', cardio_value.get('value', 'N/A'))
                            else:
                                day_data['cardio_fitness'] = cardio_value
                elif 'value' in cardio_data:
                    # Handle alternate structure
                    cardio_value = cardio_data['value']
                    if isinstance(cardio_value, dict):
                        day_data['cardio_fitness'] = cardio_value.get('vo2Max', cardio_value.get('value', 'N/A'))
                    else:
                        day_data['cardio_fitness'] = cardio_value
            
            # Temperature variation data
            print(f"  Fetching temperature data for {date_str}...")
            temp_data = get_fitbit_data(access_token, f"/1/user/-/temp/skin/date/{date_str}.json")
            print(f"  Temperature response: {temp_data}")
            
            if temp_data and temp_data.get('status') == 429:
                rate_limit_errors += 1
                print(f"⚠️  Rate limit hit on temperature call (error #{rate_limit_errors})")
            elif temp_data and not temp_data.get('error'):
                if 'tempSkin' in temp_data:
                    temp_readings = temp_data['tempSkin']
                    if isinstance(temp_readings, list) and len(temp_readings) > 0:
                        first_temp = temp_readings[0]
                        if isinstance(first_temp, dict) and 'value' in first_temp:
                            temp_value = first_temp['value']
                            if isinstance(temp_value, dict):
                                nightly_rel = temp_value.get('nightlyRelative')
                                if nightly_rel is not None:
                                    day_data['temperature_variation'] = f"{nightly_rel:+.2f}"
                elif 'value' in temp_data:
                    # Handle alternate structure
                    temp_value = temp_data['value']
                    if isinstance(temp_value, dict):
                        nightly_rel = temp_value.get('nightlyRelative')
                        if nightly_rel is not None:
                            day_data['temperature_variation'] = f"{nightly_rel:+.2f}"
            
            daily_health_data.append(day_data)
            
            print(f"  Final data for {date_str}: SpO2={day_data['spo2']}, BR={day_data['breathing_rate']}, Cardio={day_data['cardio_fitness']}, Temp={day_data['temperature_variation']}")
            
            # Add small delay between days to be more respectful to API
            time.sleep(0.5)
            
        print(f"Retrieved {len(daily_health_data)} days of health metrics data (with {rate_limit_errors} rate limit errors)")
        return daily_health_data
        
    except Exception as e:
        print(f"Error getting health metrics range data: {str(e)}")
        import traceback
        traceback.print_exc()
        return []

def get_health_metrics_data(access_token, date=None):
    """Get advanced health metrics like SpO2, breathing rate, etc."""
    health_data = {}
    
    # Convert date to proper format
    if date is None or date == "today":
        date = datetime.now().strftime('%Y-%m-%d')
    
    # SpO2 data
    spo2_data = get_fitbit_data(access_token, f"/1/user/-/spo2/date/{date}.json")
    health_data['spo2'] = spo2_data
    
    # Breathing rate
    breathing_data = get_fitbit_data(access_token, f"/1/user/-/br/date/{date}.json")
    health_data['breathing'] = breathing_data
    
    # Skin temperature
    temp_data = get_fitbit_data(access_token, f"/1/user/-/temp/skin/date/{date}.json")
    health_data['temperature'] = temp_data
    
    # Cardio fitness score
    cardio_data = get_fitbit_data(access_token, f"/1/user/-/cardioscore/date/{date}.json")
    health_data['cardio_fitness'] = cardio_data
    
    return health_data

def get_active_zone_minutes_detailed(access_token, date=None):
    """Get detailed Active Zone Minutes breakdown"""
    # Convert date to proper format
    if date is None or date == "today":
        date = datetime.now().strftime('%Y-%m-%d')
        
    azm_data = get_fitbit_data(access_token, f"/1/user/-/activities/active-zone-minutes/date/{date}/1d.json")
    return azm_data

def get_fitbit_data(access_token, endpoint):
    """Helper function to make Fitbit API requests with rate limiting and caching"""
    try:
        # Check cache first
        cached_data = get_cached_api_data(access_token, endpoint)
        if cached_data is not None:
            return cached_data
        
        # Apply rate limiting before making the request
        rate_limiter.wait_if_needed()
        
        response = requests.get(
            f"https://api.fitbit.com{endpoint}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30
        )
        
        print(f"API Call: {endpoint}")
        print(f"Status Code: {response.status_code}")
        print(f"Response Text: {response.text[:500]}...")  # First 500 chars
        
        if response.status_code == 200:
            data = response.json()
            # Cache successful responses
            cache_api_data(access_token, endpoint, data)
            return data
        elif response.status_code == 401:
            return {"error": "Authentication failed - token may be expired", "status": response.status_code}
        elif response.status_code == 429:
            # Handle rate limit specifically
            rate_limiter.handle_rate_limit_error()
            return {"error": "Rate limit exceeded - too many requests", "status": response.status_code}
        else:
            return {"error": f"API request failed", "status": response.status_code, "details": response.text}
    except Exception as e:
        print(f"Exception in API call: {str(e)}")
        return {"error": "Request failed", "details": str(e)}

@app.route("/activity/today")
def get_today_activity():
    """Get today's activity summary including steps, active zone minutes, etc."""
    # For demo purposes, using test token - in real app, store token from callback
    access_token = request.args.get("token")
    if not access_token:
        return {"error": "Access token required. Add ?token=YOUR_ACCESS_TOKEN"}, 400
    
    return get_fitbit_data(access_token, "/1/user/-/activities/date/today.json")

@app.route("/activity/steps/<date>")
def get_steps(date):
    """Get steps for a specific date (format: YYYY-MM-DD)"""
    access_token = request.args.get("token")
    if not access_token:
        return {"error": "Access token required. Add ?token=YOUR_ACCESS_TOKEN"}, 400
    
    return get_fitbit_data(access_token, f"/1/user/-/activities/steps/date/{date}/1d.json")

@app.route("/activity/active-zone-minutes/<date>")
def get_active_zone_minutes(date):
    """Get active zone minutes for a specific date (format: YYYY-MM-DD)"""
    access_token = request.args.get("token")
    if not access_token:
        return {"error": "Access token required. Add ?token=YOUR_ACCESS_TOKEN"}, 400
    
    return get_fitbit_data(access_token, f"/1/user/-/activities/active-zone-minutes/date/{date}/1d.json")

@app.route("/activity/summary/<days>")
def get_activity_summary(days):
    """Get activity summary for past X days"""
    access_token = request.args.get("token")
    if not access_token:
        return {"error": "Access token required. Add ?token=YOUR_ACCESS_TOKEN"}, 400
    
    from datetime import datetime, timedelta
    
    try:
        num_days = int(days)
        if num_days > 365:  # Fitbit API limit
            return {"error": "Maximum 365 days allowed"}, 400
    except ValueError:
        return {"error": "Invalid number of days"}, 400
    
    # Get data for the specified number of days
    end_date = datetime.now()
    start_date = end_date - timedelta(days=num_days-1)
    
    date_range = f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"
    
    # Get multiple data types
    data = {}
    
    # Steps
    steps_data = get_fitbit_data(access_token, f"/1/user/-/activities/steps/date/{date_range}.json")
    data["steps"] = steps_data
    
    # Active zone minutes
    azm_data = get_fitbit_data(access_token, f"/1/user/-/activities/active-zone-minutes/date/{date_range}.json")
    data["active_zone_minutes"] = azm_data
    
    # Distance
    distance_data = get_fitbit_data(access_token, f"/1/user/-/activities/distance/date/{date_range}.json")
    data["distance"] = distance_data
    
    # Calories burned
    calories_data = get_fitbit_data(access_token, f"/1/user/-/activities/calories/date/{date_range}.json")
    data["calories"] = calories_data
    
    return data

@app.route("/help")
def help_endpoints():
    """Show available endpoints and how to use them"""
    return {
        "message": "Fitbit Health Analysis API",
        "endpoints": {
            "/login": "Start OAuth flow to get access token",
            "/callback": "OAuth callback (automatic)",
            "/activity/today?token=ACCESS_TOKEN": "Get today's activity summary",
            "/activity/steps/YYYY-MM-DD?token=ACCESS_TOKEN": "Get steps for specific date",
            "/activity/active-zone-minutes/YYYY-MM-DD?token=ACCESS_TOKEN": "Get active zone minutes for specific date",
            "/activity/summary/DAYS?token=ACCESS_TOKEN": "Get activity summary for past X days (max 365)",
            "/help": "This help message"
        },
        "example_usage": [
            "http://localhost:3000/activity/today?token=YOUR_ACCESS_TOKEN",
            "http://localhost:3000/activity/steps/2025-09-17?token=YOUR_ACCESS_TOKEN",
            "http://localhost:3000/activity/summary/7?token=YOUR_ACCESS_TOKEN"
        ],
        "note": "Get your ACCESS_TOKEN by completing the OAuth flow at /login first"
    }

# ============================================================================
# HEALTH SCORE API ENDPOINTS
# ============================================================================

@app.route("/api/health-score")
def get_health_score_api():
    """Get cached health score or return status indicating calculation needed"""
    if 'access_token' not in session:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        # Get user ID and cache
        user_id = get_user_id_from_session(session)
        cache = get_health_score_cache()
        
        # Try to get cached score (default 7 days)
        days = request.args.get('days', 7, type=int)
        cached_score = cache.get_cached_score(user_id, days)
        
        if cached_score:
            return jsonify({
                "cached": True,
                "score_data": cached_score['score_data'],
                "calculated_at": cached_score['cached_at'],
                "expires_at": cached_score['expires_at']
            })
        else:
            return jsonify({
                "cached": False,
                "needs_calculation": True,
                "message": "No cached score available. Use /api/health-score/calculate to generate."
            })
    
    except Exception as e:
        return jsonify({"error": f"Error retrieving health score: {str(e)}"}), 500

@app.route("/api/health-score/calculate", methods=['POST'])
def calculate_health_score_api():
    """Trigger health score calculation with progress tracking"""
    if 'access_token' not in session:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        # Get parameters
        days = request.json.get('days', 7) if request.json else 7
        
        if days not in [7, 30, 90]:
            return jsonify({"error": "Days must be 7, 30, or 90"}), 400
        
        # Generate session ID for tracking
        session_id = str(uuid.uuid4())
        
        # Initialize progress tracking
        health_score_progress[session_id] = {
            'status': 'starting',
            'percentage': 0,
            'message': 'Initializing health analysis...',
            'started_at': datetime.now().isoformat(),
            'days': days
        }
        
        # Start calculation in background thread
        thread = threading.Thread(
            target=_calculate_health_score_background,
            args=(session_id, session.get('access_token'), session.get('refresh_token'), days, get_user_id_from_session(session))
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "session_id": session_id,
            "status": "calculation_started",
            "message": "Health score calculation started. Use the session_id to track progress."
        })
    
    except Exception as e:
        return jsonify({"error": f"Error starting calculation: {str(e)}"}), 500

@app.route("/api/health-score/progress/<session_id>")
def get_health_score_progress(session_id):
    """Get progress of health score calculation"""
    if session_id not in health_score_progress:
        return jsonify({"error": "Invalid session ID"}), 404
    
    progress = health_score_progress[session_id]
    
    # Check if calculation is complete
    if progress['status'] == 'completed' and session_id in health_score_results:
        result = health_score_results[session_id]
        
        # Clean up tracking data after returning results
        del health_score_progress[session_id]
        del health_score_results[session_id]
        
        return jsonify({
            "status": "completed",
            "percentage": 100,
            "message": "Health score calculation completed!",
            "result": result
        })
    
    elif progress['status'] == 'error':
        error_msg = progress.get('error', 'Unknown error occurred')
        
        # Clean up tracking data
        del health_score_progress[session_id]
        
        return jsonify({
            "status": "error",
            "message": f"Calculation failed: {error_msg}"
        })
    
    else:
        # Return current progress
        return jsonify({
            "status": progress['status'],
            "percentage": progress['percentage'],
            "message": progress['message']
        })

@app.route("/health-score/details")
def health_score_details():
    """Health score details page with breakdown"""
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    # Get requested days (default 7)
    days = request.args.get('days', 7, type=int)
    if days not in [7, 30, 90]:
        days = 7
    
    # Try to get cached score
    user_id = get_user_id_from_session(session)
    cache = get_health_score_cache()
    cached_score = cache.get_cached_score(user_id, days)
    
    profile = session.get('profile', {})
    
    return render_template(
        'health_score_details.html',
        profile=profile,
        score_data=cached_score['score_data'] if cached_score else None,
        days=days,
        cached_at=cached_score['cached_at'] if cached_score else None
    )

def refresh_token_standalone(access_token: str, refresh_token: str):
    """Standalone token refresh function for background threads"""
    if not refresh_token:
        return None
        
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    
    try:
        print(f"🔄 Background thread refreshing Fitbit access token...")
        token_res = requests.post(
            "https://api.fitbit.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        
        if token_res.status_code != 200:
            print(f"❌ Background token refresh failed: {token_res.status_code} - {token_res.text}")
            return None

        token_data = token_res.json()
        if "access_token" not in token_data:
            print("❌ No access token in background refresh response")
            return None
            
        print(f"✅ Background thread successfully refreshed access token")
        return token_data["access_token"]
        
    except Exception as e:
        print(f"❌ Error refreshing token in background: {e}")
        return None

def _calculate_health_score_background(session_id: str, access_token: str, refresh_token: str, days: int, user_id: str):
    """Background function to calculate health score with progress updates"""
    
    def update_progress(percentage: int, message: str, status: str = 'in_progress'):
        health_score_progress[session_id] = {
            'status': status,
            'percentage': percentage,
            'message': message,
            'started_at': health_score_progress[session_id]['started_at'],
            'days': days
        }
    
    try:
        # First, check and refresh token if needed
        update_progress(5, "Validating authentication...")
        current_token = access_token
        
        # Try the current token first
        test_response = requests.get(
            "https://api.fitbit.com/1/user/-/profile.json",
            headers={"Authorization": f"Bearer {current_token}"},
            timeout=10
        )
        
        if test_response.status_code == 401:
            print("🔄 Access token expired, attempting refresh...")
            refreshed_token = refresh_token_standalone(current_token, refresh_token)
            if refreshed_token:
                current_token = refreshed_token
                print("✅ Successfully refreshed token for background calculation")
            else:
                print("❌ Failed to refresh token")
                update_progress(0, "Authentication failed - please log in again", 'error')
                health_score_results[session_id] = {
                    'status': 'error',
                    'message': 'Authentication failed - please log in again',
                    'result': None
                }
                return
        
        # Step 1: Gather health data
        update_progress(10, "Gathering your health data...")
        
        health_data = {}
        data_fetch_errors = []
        
        # Activity data
        update_progress(20, "Fetching activity data...")
        try:
            activity_data = get_activity_range_data(current_token, days, 'all')
            if activity_data:
                health_data['activity_data'] = _process_activity_data(activity_data)
        except Exception as e:
            error_msg = f"Error fetching activity data: {str(e)}"
            print(error_msg)
            data_fetch_errors.append("Activity data")
        
        # Heart rate data
        update_progress(30, "Fetching heart rate data...")
        try:
            heart_data = get_heart_rate_range_data(current_token, days)
            print(f"🔍 Raw heart rate data retrieved: {len(heart_data) if heart_data else 0} days")
            if heart_data and len(heart_data) > 0:
                print(f"🔍 Sample heart rate day: {heart_data[0]}")
                processed_heart_data = _process_heart_data(heart_data)
                health_data['heart_data'] = processed_heart_data
                print(f"🔍 Processed heart data: {processed_heart_data}")
            else:
                print("🔍 No heart rate data available")
                data_fetch_errors.append("Heart rate data")
        except Exception as e:
            print(f"Error fetching heart data: {str(e)}")
            data_fetch_errors.append("Heart rate data")
            import traceback
            traceback.print_exc()
        
        # Sleep data
        update_progress(40, "Fetching sleep data...")
        try:
            sleep_data = get_sleep_range_data(current_token, days)
            if sleep_data:
                health_data['sleep_data'] = _process_sleep_data(sleep_data)
            else:
                data_fetch_errors.append("Sleep data")
        except Exception as e:
            print(f"Error fetching sleep data: {str(e)}")
            data_fetch_errors.append("Sleep data")
        
        # Body data
        update_progress(50, "Fetching body metrics...")
        try:
            body_data = get_body_range_data(current_token, days)
            if body_data:
                health_data['body_data'] = _process_body_data(body_data)
            else:
                data_fetch_errors.append("Body data")
        except Exception as e:
            print(f"Error fetching body data: {str(e)}")
            data_fetch_errors.append("Body data")
        
        # Exercise data
        update_progress(60, "Fetching exercise data...")
        try:
            exercise_data = get_exercise_range_data_enhanced(current_token, days)
            print(f"🔍 Raw exercise data retrieved: {len(exercise_data) if exercise_data else 0} activities")
            if exercise_data and len(exercise_data) > 0:
                print(f"🔍 Sample exercise activity: {exercise_data[0]}")
                processed_exercise_data = _process_exercise_data(exercise_data)
                health_data['exercise_data'] = processed_exercise_data
                print(f"🔍 Processed exercise data: {processed_exercise_data}")
            else:
                print("🔍 No exercise data available")
                data_fetch_errors.append("Exercise data")
        except Exception as e:
            print(f"Error fetching exercise data: {str(e)}")
            data_fetch_errors.append("Exercise data")
            import traceback
            traceback.print_exc()
        
        # Health metrics data
        update_progress(70, "Fetching advanced health metrics...")
        try:
            health_metrics_data = get_health_metrics_range_data_optimized_v2(current_token, days)
            if health_metrics_data:
                health_data['health_metrics'] = _process_health_metrics_data(health_metrics_data)
            else:
                data_fetch_errors.append("Health metrics data")
        except Exception as e:
            print(f"Error fetching health metrics data: {str(e)}")
            data_fetch_errors.append("Health metrics data")
        
        # Check if we have enough data to continue
        if len(data_fetch_errors) >= 5:  # If we failed to get most categories
            update_progress(0, "Too many API errors - rate limit exceeded. Please try again later.", 'error')
            health_score_results[session_id] = {
                'status': 'error',
                'message': 'Rate limit exceeded - too many API errors. Please try again in a few minutes.',
                'result': None
            }
            return
        elif len(data_fetch_errors) > 0:
            # Continue with partial data but notify user
            missing_data_msg = f"Proceeding with partial data (missing: {', '.join(data_fetch_errors)})"
            print(f"⚠️  {missing_data_msg}")
            update_progress(75, missing_data_msg)
        
        # Step 2: Analyze with Gemini
        update_progress(80, "Analyzing with AI...")
        
        # Get analyzer and validate data
        analyzer = get_health_analyzer()
        is_valid, reason = analyzer.validate_health_data(health_data)
        
        if not is_valid:
            update_progress(0, f"Insufficient data: {reason}", 'error')
            health_score_progress[session_id]['error'] = reason
            return
        
        # Perform analysis
        update_progress(90, "Calculating your health score...")
        score_result = analyzer.analyze_health_data(health_data, days)
        
        # Add raw health data for detailed modal display
        if 'raw_data' not in score_result:
            score_result['raw_data'] = health_data
        
        # Step 3: Cache the result
        update_progress(95, "Finalizing results...")
        cache = get_health_score_cache()
        cache.store_score(user_id, score_result, days)
        
        # Store result for retrieval
        health_score_results[session_id] = score_result
        
        # Mark as completed
        update_progress(100, "Health score calculation completed!", 'completed')
        
    except Exception as e:
        error_msg = str(e)
        print(f"Health score calculation error: {error_msg}")
        health_score_progress[session_id] = {
            'status': 'error',
            'error': error_msg,
            'started_at': health_score_progress[session_id]['started_at'],
            'days': days
        }

def _process_activity_data(activity_data):
    """Process activity data for AI analysis"""
    if not activity_data:
        return None
    
    steps_values = []
    calories_values = []
    distance_values = []
    active_days = 0
    
    for day in activity_data:
        if isinstance(day, dict):
            # Handle steps (can be string or number from API)
            steps = day.get('steps', 0)
            if steps != 'N/A' and steps is not None:
                try:
                    steps_int = int(float(str(steps)))  # Convert to int, handle string numbers
                    if steps_int > 0:
                        steps_values.append(steps_int)
                        if steps_int > 1000:  # Consider active if more than 1000 steps
                            active_days += 1
                except (ValueError, TypeError):
                    pass  # Skip invalid values
            
            # Handle calories (can be string or number from API)
            calories = day.get('calories', 0)
            if calories != 'N/A' and calories is not None:
                try:
                    calories_float = float(str(calories))  # Convert to float, handle string numbers
                    if calories_float > 0:
                        calories_values.append(calories_float)
                except (ValueError, TypeError):
                    pass  # Skip invalid values
            
            # Handle distance (can be string or number from API)
            distance = day.get('distance', 0)
            if distance != 'N/A' and distance is not None:
                try:
                    distance_float = float(str(distance))  # Convert to float, handle string numbers
                    if distance_float > 0:
                        distance_values.append(distance_float)
                except (ValueError, TypeError):
                    pass  # Skip invalid values
    
    return {
        'daily_steps': steps_values,
        'average_steps': sum(steps_values) / len(steps_values) if steps_values else 0,
        'total_steps': sum(steps_values),
        'daily_calories': calories_values,
        'average_calories': sum(calories_values) / len(calories_values) if calories_values else 0,
        'daily_distance': distance_values,
        'average_distance': sum(distance_values) / len(distance_values) if distance_values else 0,
        'active_days': active_days,
        'total_days': len(activity_data)
    }

def _process_heart_data(heart_data):
    """Process heart rate data for AI analysis"""
    if not heart_data:
        return None
    
    resting_hr_values = []
    hrv_values = []
    zone_minutes = {'fat_burn': 0, 'cardio': 0, 'peak': 0, 'out_of_range': 0}
    total_zone_minutes = 0
    
    for day in heart_data:
        if isinstance(day, dict):
            # Process resting heart rate
            resting_hr = day.get('resting_hr')
            if resting_hr != 'N/A' and isinstance(resting_hr, (int, float)) and resting_hr > 0:
                resting_hr_values.append(resting_hr)
            
            # Process HRV data
            hrv = day.get('hrv_rmssd')
            if hrv != 'N/A' and isinstance(hrv, (int, float)) and hrv > 0:
                hrv_values.append(hrv)
            
            # Process heart rate zones
            zones = day.get('zones', {})
            for zone_name, minutes in zones.items():
                if isinstance(minutes, (int, float)) and minutes > 0:
                    zone_name_lower = zone_name.lower()
                    if 'fat' in zone_name_lower or 'burn' in zone_name_lower:
                        zone_minutes['fat_burn'] += minutes
                    elif 'cardio' in zone_name_lower:
                        zone_minutes['cardio'] += minutes
                    elif 'peak' in zone_name_lower or 'vigorous' in zone_name_lower:
                        zone_minutes['peak'] += minutes
                    elif 'out of range' in zone_name_lower or 'below' in zone_name_lower:
                        zone_minutes['out_of_range'] += minutes
                    
                    total_zone_minutes += minutes
    
    # Calculate summary statistics
    processed_data = {
        'resting_hr_values': resting_hr_values,
        'avg_resting_hr': round(sum(resting_hr_values) / len(resting_hr_values), 1) if resting_hr_values else 0,
        'min_resting_hr': min(resting_hr_values) if resting_hr_values else 0,
        'max_resting_hr': max(resting_hr_values) if resting_hr_values else 0,
        'hrv_values': hrv_values,
        'avg_hrv': round(sum(hrv_values) / len(hrv_values), 1) if hrv_values else 0,
        'zone_minutes': zone_minutes,
        'total_active_minutes': sum([zone_minutes[k] for k in ['fat_burn', 'cardio', 'peak']]),
        'total_zone_minutes': total_zone_minutes,
        'days_with_hr_data': len(resting_hr_values),
        'days_with_hrv_data': len(hrv_values)
    }
    
    print(f"🫀 Processed heart data: {processed_data['days_with_hr_data']} days HR, {processed_data['days_with_hrv_data']} days HRV")
    print(f"🫀 Avg resting HR: {processed_data['avg_resting_hr']}, Avg HRV: {processed_data['avg_hrv']}")
    print(f"🫀 Zone minutes: {processed_data['zone_minutes']}")
    
    return processed_data

def _process_sleep_data(sleep_data):
    """Process sleep data for AI analysis"""
    if not sleep_data:
        return None
    
    sleep_hours = []
    efficiency_values = []
    deep_minutes = []
    rem_minutes = []
    
    for day in sleep_data:
        if isinstance(day, dict):
            total_hours = day.get('total_hours')
            if isinstance(total_hours, (int, float)) and total_hours > 0:
                sleep_hours.append(total_hours)
            
            efficiency = day.get('efficiency')
            if isinstance(efficiency, (int, float)) and efficiency > 0:
                efficiency_values.append(efficiency)
            
            deep_min = day.get('deep_minutes')
            if isinstance(deep_min, (int, float)) and deep_min > 0:
                deep_minutes.append(deep_min)
            
            rem_min = day.get('rem_minutes')
            if isinstance(rem_min, (int, float)) and rem_min > 0:
                rem_minutes.append(rem_min)
    
    return {
        'daily_sleep_hours': sleep_hours,
        'avg_sleep_hours': sum(sleep_hours) / len(sleep_hours) if sleep_hours else 0,
        'efficiency_values': efficiency_values,
        'avg_efficiency': sum(efficiency_values) / len(efficiency_values) if efficiency_values else 0,
        'avg_deep_minutes': sum(deep_minutes) / len(deep_minutes) if deep_minutes else 0,
        'avg_rem_minutes': sum(rem_minutes) / len(rem_minutes) if rem_minutes else 0
    }

def _process_body_data(body_data):
    """Process body metrics data for AI analysis"""
    if not body_data:
        return None
    
    weight_values = []
    bmi_values = []
    fat_percentage_values = []
    
    for day in body_data:
        if isinstance(day, dict):
            weight = day.get('weight')
            if isinstance(weight, (int, float)) and weight > 0:
                weight_values.append(weight)
            
            bmi = day.get('bmi')
            if isinstance(bmi, (int, float)) and bmi > 0:
                bmi_values.append(bmi)
            
            fat_pct = day.get('fat_percentage')
            if isinstance(fat_pct, (int, float)) and fat_pct > 0:
                fat_percentage_values.append(fat_pct)
    
    return {
        'weight_values': weight_values,
        'avg_weight': sum(weight_values) / len(weight_values) if weight_values else 0,
        'weight_trend': 'stable' if len(weight_values) < 2 else ('increasing' if weight_values[-1] > weight_values[0] else 'decreasing'),
        'bmi_values': bmi_values,
        'avg_bmi': sum(bmi_values) / len(bmi_values) if bmi_values else 0,
        'fat_percentage_values': fat_percentage_values,
        'avg_fat_percentage': sum(fat_percentage_values) / len(fat_percentage_values) if fat_percentage_values else 0
    }

def _process_exercise_data(exercise_data):
    """Process exercise data for AI analysis"""
    if not exercise_data:
        print("🏋️‍♂️ No exercise data to process")
        return None
    
    print(f"🏋️‍♂️ Processing {len(exercise_data)} exercise activities")
    
    workout_count = 0
    total_calories = 0
    total_minutes = 0
    activity_types = set()
    activities_by_day = {}
    
    for activity in exercise_data:
        if isinstance(activity, dict):
            workout_count += 1
            
            # Process calories
            calories = activity.get('calories', 0)
            if calories != 'N/A' and isinstance(calories, (int, float)):
                total_calories += calories
            
            # Process duration
            duration = activity.get('duration_minutes', 0)
            if duration != 'N/A' and isinstance(duration, (int, float)):
                total_minutes += duration
            
            # Process activity type
            activity_name = activity.get('name', 'Unknown')
            if activity_name and activity_name != 'Unknown':
                activity_types.add(activity_name)
            
            # Group by date for variety analysis
            date_str = activity.get('date_str', activity.get('date', 'unknown'))
            if date_str not in activities_by_day:
                activities_by_day[date_str] = []
            activities_by_day[date_str].append(activity)
    
    # Calculate additional metrics
    days_with_exercise = len(activities_by_day)
    avg_workouts_per_active_day = workout_count / days_with_exercise if days_with_exercise > 0 else 0
    
    processed_data = {
        'total_workouts': workout_count,
        'total_exercise_calories': round(total_calories),
        'total_exercise_minutes': round(total_minutes, 1),
        'avg_minutes_per_workout': round(total_minutes / workout_count, 1) if workout_count > 0 else 0,
        'activity_variety': len(activity_types),
        'activity_types': list(activity_types),
        'days_with_exercise': days_with_exercise,
        'avg_workouts_per_active_day': round(avg_workouts_per_active_day, 1),
        'weekly_exercise_minutes': round(total_minutes * (7 / max(days_with_exercise, 1)), 1) if days_with_exercise > 0 else 0
    }
    
    print(f"🏋️‍♂️ Processed exercise data: {processed_data['total_workouts']} workouts, {processed_data['total_exercise_minutes']} min total")
    print(f"🏋️‍♂️ Activity variety: {processed_data['activity_variety']} types - {processed_data['activity_types']}")
    print(f"🏋️‍♂️ Days with exercise: {processed_data['days_with_exercise']}")
    
    return processed_data

def _process_health_metrics_data(health_metrics_data):
    """Process advanced health metrics for AI analysis"""
    if not health_metrics_data:
        return None
    
    spo2_values = []
    breathing_rate_values = []
    vo2_max_values = []
    temp_variation_values = []
    
    for day in health_metrics_data:
        if isinstance(day, dict):
            spo2 = day.get('spo2')
            if isinstance(spo2, (int, float)) and spo2 > 0:
                spo2_values.append(spo2)
            
            breathing_rate = day.get('breathing_rate')
            if isinstance(breathing_rate, (int, float)) and breathing_rate > 0:
                breathing_rate_values.append(breathing_rate)
            
            cardio_fitness = day.get('cardio_fitness')
            if isinstance(cardio_fitness, (int, float)) and cardio_fitness > 0:
                vo2_max_values.append(cardio_fitness)
            
            temp_var = day.get('temperature_variation')
            if isinstance(temp_var, str) and temp_var not in ['N/A', '']:
                try:
                    temp_val = float(temp_var.replace('+', '').replace('°', ''))
                    temp_variation_values.append(temp_val)
                except (ValueError, AttributeError):
                    pass
    
    return {
        'spo2_values': spo2_values,
        'avg_spo2': sum(spo2_values) / len(spo2_values) if spo2_values else 0,
        'breathing_rate_values': breathing_rate_values,
        'avg_breathing_rate': sum(breathing_rate_values) / len(breathing_rate_values) if breathing_rate_values else 0,
        'vo2_max_values': vo2_max_values,
        'avg_vo2_max': sum(vo2_max_values) / len(vo2_max_values) if vo2_max_values else 0,
        'temperature_variations': temp_variation_values
    }

@app.route("/debug/health-data")
@debug_only
def debug_health_data():
    """Debug endpoint to see what health data we're actually retrieving"""
    if 'access_token' not in session:
        return jsonify({"error": "Not authenticated", "message": "Please login first at http://localhost:3000"}), 401
    
    try:
        access_token = get_valid_access_token()
        if not access_token:
            return jsonify({"error": "Token refresh failed", "message": "Please re-authenticate"}), 401
    except:
        access_token = session.get('access_token')
        if not access_token:
            return jsonify({"error": "No access token in session", "message": "Please login first"}), 401
    
    debug_info = {
        'authentication': 'success',
        'user_id': get_user_id_from_session(session),
        'data_samples': {}
    }
    
    try:
        # Test specifically heart rate data since that's the issue
        days = 7
        
        # Heart rate data - the focus of this debug
        debug_info['heart_rate_debug'] = {}
        try:
            print("🔍 DEBUG: Testing heart rate data retrieval...")
            heart_data = get_heart_rate_range_data(access_token, days)
            debug_info['heart_rate_debug']['raw_data_count'] = len(heart_data) if heart_data else 0
            debug_info['heart_rate_debug']['raw_sample'] = heart_data[:2] if heart_data else []
            
            if heart_data:
                processed = _process_heart_data(heart_data)
                debug_info['heart_rate_debug']['processed_data'] = processed
            else:
                debug_info['heart_rate_debug']['processed_data'] = None
                
        except Exception as e:
            debug_info['heart_rate_debug']['error'] = str(e)
            import traceback
            debug_info['heart_rate_debug']['traceback'] = traceback.format_exc()
        
        # Quick test of other data types
        try:
            activity_data = get_activity_range_data(access_token, days, 'all')
            debug_info['data_samples']['activity'] = {
                'status': 'success',
                'count': len(activity_data) if activity_data else 0
            }
        except Exception as e:
            debug_info['data_samples']['activity'] = {'status': 'error', 'error': str(e)}
        
        try:
            sleep_data = get_sleep_range_data(access_token, days)
            debug_info['data_samples']['sleep'] = {
                'status': 'success',
                'count': len(sleep_data) if sleep_data else 0
            }
        except Exception as e:
            debug_info['data_samples']['sleep'] = {'status': 'error', 'error': str(e)}
            
    except Exception as e:
        debug_info['general_error'] = str(e)
        import traceback
        debug_info['traceback'] = traceback.format_exc()
    
    return jsonify(debug_info)

@app.route("/trigger-health-score")
def trigger_health_score():
    """Manually trigger health score calculation"""
    if 'access_token' not in session:
        return "Not authenticated. <a href='/login'>Login</a>"
    
    import requests as req
    data = {'days': 7}
    try:
        response = req.post(
            'http://localhost:3000/api/health-score/calculate',
            json=data,
            cookies=request.cookies
        )
        return f"Health score calculation triggered: {response.json()}"
    except Exception as e:
        return f"Error triggering calculation: {str(e)}"

@app.route("/test-rate-limiter")
@debug_only
def test_rate_limiter():
    """Test the rate limiter functionality"""
    if 'access_token' not in session:
        return "Not authenticated. <a href='/login'>Login</a>"
    
    results = []
    access_token = get_valid_access_token(session.get('access_token'), session.get('refresh_token'))
    
    if not access_token:
        return "Failed to get valid access token"
    
    # Make a few test calls to see rate limiting in action
    for i in range(3):
        import time
        start_time = time.time()
        result = get_fitbit_data(access_token, f"/1/user/-/profile.json")
        end_time = time.time()
        
        results.append({
            'call': i+1,
            'duration': f"{end_time - start_time:.2f}s",
            'success': not result.get('error'),
            'cached': '📦 Using cached data' in str(result)
        })
    
    return f"""
    <h2>Rate Limiter Test Results</h2>
    <pre>{results}</pre>
    <p><a href="/dashboard">Back to Dashboard</a></p>
    """

@app.route("/clear-cache")
def clear_cache():
    """Clear all caches for testing"""
    if 'access_token' not in session:
        return "<h1>Not authenticated</h1><p>Go to <a href='/'>home</a> and login first</p>"
    
    try:
        # Clear in-memory caches
        global fitbit_api_cache, exercise_cache, health_metrics_cache
        fitbit_api_cache.clear()
        exercise_cache.clear() 
        health_metrics_cache.clear()
        
        # Clear health score cache files
        import os
        cache_dir = 'cache'
        cleared_files = 0
        if os.path.exists(cache_dir):
            for file in os.listdir(cache_dir):
                if file.startswith('health_score_') and file.endswith('.json'):
                    os.remove(os.path.join(cache_dir, file))
                    print(f"Deleted cache file: {file}")
                    cleared_files += 1
        
        # Also clear health score cache
        cache = get_health_score_cache()
        cache.cache.clear()
        
        return f"""
        <h2>Cache Cleared Successfully! ✅</h2>
        <p>Cleared {cleared_files} health score cache files</p>
        <p>Cleared all in-memory API caches</p>
        <p><a href="/dashboard">Back to Dashboard</a></p>
        <p><a href="/trigger-health-score">Trigger New Health Score</a></p>
        """
        
    except Exception as e:
        return f"""
        <h2>Error Clearing Cache ❌</h2>
        <p>Error: {str(e)}</p>
        <p><a href="/dashboard">Back to Dashboard</a></p>
        """

@app.route("/test-exercise-data")
@debug_only
def test_exercise_data():
    """Simple test to see exercise data structure"""
    import json
    
    if 'access_token' not in session:
        return "<h1>Not authenticated</h1><p>Go to <a href='/'>home</a> and login first</p>"
    
    access_token = session.get('access_token')
    try:
        # Get exercise data using enhanced method
        exercise_data = get_exercise_range_data_enhanced(access_token, 7)
        
        if not exercise_data:
            return "<h1>No Exercise Data</h1><p>Exercise data retrieval returned empty</p>"
        
        # Process the data using our current processing function
        processed = _process_exercise_data(exercise_data)
        
        html = f"""
        <h1>Exercise Data Test</h1>
        <h2>Raw Data Sample (first 3 activities):</h2>
        <pre>{json.dumps(exercise_data[:3], indent=2)}</pre>
        
        <h2>Current Processing Result:</h2>
        <pre>{json.dumps(processed, indent=2)}</pre>
        
        <h2>Summary:</h2>
        <p>Total activities retrieved: {len(exercise_data)}</p>
        <p>Data structure type: {type(exercise_data[0]) if exercise_data else 'N/A'}</p>
        <p>Sample keys: {list(exercise_data[0].keys()) if exercise_data and isinstance(exercise_data[0], dict) else 'N/A'}</p>
        """
        return html
        
    except Exception as e:
        import traceback
        return f"<h1>Error</h1><pre>{str(e)}\n\n{traceback.format_exc()}</pre>"

@app.route("/test-heart-data")
@debug_only
def test_heart_data():
    """Simple test to see heart rate data"""
    import json
    
    if 'access_token' not in session:
        return "<h1>Not authenticated</h1><p>Go to <a href='/'>home</a> and login first</p>"
    
    access_token = session.get('access_token')
    try:
        # Get heart rate data
        heart_data = get_heart_rate_range_data(access_token, 7)
        
        if not heart_data:
            return "<h1>No Heart Rate Data</h1><p>Heart rate data retrieval returned empty</p>"
        
        # Process the data
        processed = _process_heart_data(heart_data)
        
        html = f"""
        <h1>Heart Rate Data Test</h1>
        <h2>Raw Data Sample (first 2 days):</h2>
        <pre>{json.dumps(heart_data[:2], indent=2)}</pre>
        
        <h2>Processed Data:</h2>
        <pre>{json.dumps(processed, indent=2)}</pre>
        
        <h2>Summary:</h2>
        <p>Total days with data: {len(heart_data)}</p>
        <p>Days with HR data: {processed.get('days_with_hr_data', 0) if processed else 0}</p>
        <p>Average resting HR: {processed.get('avg_resting_hr', 'N/A') if processed else 'N/A'}</p>
        <p>Average HRV: {processed.get('avg_hrv', 'N/A') if processed else 'N/A'}</p>
        """
        return html
        
    except Exception as e:
        import traceback
        return f"<h1>Error</h1><pre>{str(e)}\n\n{traceback.format_exc()}</pre>"

if __name__ == "__main__":
    # For development only
    app.run(port=3000, debug=True)
    # For production, use:
    # app.run(port=3000, debug=False, host='0.0.0.0')
