"""
Gemini AI Health Score Analyzer
Integrates with Google's Gemini API to provide intelligent health scoring based on Fitbit data
"""

import json
import os
from datetime import datetime, timedelta
import google.generativeai as genai
from typing import Dict, Any, Optional, Tuple


class GeminiHealthAnalyzer:
    """Handles health data analysis using Google's Gemini API"""
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the Gemini Health Analyzer
        
        Args:
            api_key: Google AI API key. If None, will try to get from environment
        """
        self.api_key = api_key or os.getenv('GEMINI_API_KEY')
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set and no API key provided")
        
        # Configure Gemini
        genai.configure(api_key=self.api_key)
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Configuration
        self.generation_config = {
            'temperature': 0.3,  # Lower temperature for consistent scoring
            'top_p': 0.8,
            'top_k': 40,
            'max_output_tokens': 2048,
        }
    
    def analyze_health_data(self, health_data: Dict[str, Any], days: int = 7) -> Dict[str, Any]:
        """
        Analyze health data and return a comprehensive health score
        
        Args:
            health_data: Dictionary containing all available health metrics
            days: Number of days the data covers
            
        Returns:
            Dictionary containing overall score, breakdown, and explanations
        """
        try:
            # Create the prompt
            prompt = self._create_health_analysis_prompt(health_data, days)
            
            # Send to Gemini
            response = self.model.generate_content(
                prompt,
                generation_config=self.generation_config
            )
            
            # Parse and validate response
            result = self._parse_gemini_response(response.text)
            
            # Add metadata
            result['analyzed_at'] = datetime.now().isoformat()
            result['data_period_days'] = days
            
            return result
            
        except Exception as e:
            print(f"Error in Gemini analysis: {str(e)}")
            return self._create_fallback_score(health_data)
    
    def _create_health_analysis_prompt(self, health_data: Dict[str, Any], days: int) -> str:
        """Create a structured prompt for Gemini analysis"""
        
        # Calculate data completeness
        available_metrics = []
        if health_data.get('activity_data'):
            available_metrics.append("activity")
        if health_data.get('heart_data'):
            available_metrics.append("heart_health")  
        if health_data.get('sleep_data'):
            available_metrics.append("sleep_quality")
        if health_data.get('body_data'):
            available_metrics.append("body_metrics")
        if health_data.get('exercise_data'):
            available_metrics.append("exercise")
        if health_data.get('health_metrics'):
            available_metrics.append("health_metrics")
        
        data_completeness = (len(available_metrics) / 6) * 100
        
        prompt = f"""You are a health analysis expert. Analyze the following health data from the past {days} days and provide a comprehensive health score from 1-100.

IMPORTANT: Some metrics may be missing. Adjust your scoring based only on available data ({len(available_metrics)} out of 6 categories available).

Available Health Data:
{json.dumps(health_data, indent=2)}

Scoring Guidelines:
- 90-100: Excellent health indicators across available metrics
- 70-89: Good health with minor areas for improvement  
- 50-69: Average health with several areas needing attention
- 30-49: Below average health requiring significant improvements
- 1-29: Poor health indicators requiring immediate attention

Consider these factors for available data:
1. Activity levels vs recommended (8,000+ steps/day optimal)
2. Heart health (resting HR 60-100 bpm, good HRV trends)
3. Sleep quality and duration (7-9 hours optimal, 85%+ efficiency)
4. Body composition trends (stable weight, healthy BMI 18.5-24.9)
5. Exercise consistency and variety (150+ minutes/week recommended)
6. Advanced metrics (SpO2 >95%, normal breathing rate 12-20/min)

Provide your response in this exact JSON format:
{{
    "overall_score": 85,
    "data_completeness_percentage": {data_completeness:.1f},
    "breakdown": {{
        "activity": {{
            "mini_score": 88,
            "explanation": "Your daily steps average of 8,500 exceeds recommendations. Consistent activity across {days} days shows excellent exercise habits that contribute positively to cardiovascular health."
        }},
        "heart_health": {{
            "mini_score": 0,
            "explanation": "No heart rate data available for this period."
        }},
        "sleep_quality": {{
            "mini_score": 75,
            "explanation": "Your average sleep duration meets recommendations but sleep efficiency could be improved for better recovery."
        }},
        "body_metrics": {{
            "mini_score": 82,
            "explanation": "BMI and weight trends are within healthy ranges showing good body composition management."
        }},
        "exercise": {{
            "mini_score": 85,
            "explanation": "Regular workout schedule with good variety supports overall fitness and health goals."
        }},
        "health_metrics": {{
            "mini_score": 90,
            "explanation": "SpO2 and other advanced metrics indicate excellent respiratory and cardiovascular function."
        }}
    }},
    "scoring_note": "Score based on {len(available_metrics)} out of 6 available health metric categories.",
    "key_strengths": ["List your top 2-3 positive health indicators"],
    "improvement_areas": ["List 1-2 areas that could be enhanced"]
}}

CRITICAL: Respond only with valid JSON. Do not include any text before or after the JSON."""

        return prompt
    
    def _parse_gemini_response(self, response_text: str) -> Dict[str, Any]:
        """Parse and validate Gemini's JSON response"""
        try:
            # Clean the response text
            response_text = response_text.strip()
            
            # Remove any markdown formatting if present
            if response_text.startswith('```json'):
                response_text = response_text[7:]
            if response_text.endswith('```'):
                response_text = response_text[:-3]
            
            # Parse JSON
            result = json.loads(response_text.strip())
            
            # Validate required fields
            required_fields = ['overall_score', 'breakdown', 'data_completeness_percentage']
            for field in required_fields:
                if field not in result:
                    raise ValueError(f"Missing required field: {field}")
            
            # Ensure overall_score is within valid range
            if not (1 <= result['overall_score'] <= 100):
                result['overall_score'] = max(1, min(100, result['overall_score']))
            
            return result
            
        except json.JSONDecodeError as e:
            print(f"JSON parsing error: {str(e)}")
            print(f"Response text: {response_text[:500]}...")
            raise ValueError("Invalid JSON response from Gemini")
        except Exception as e:
            print(f"Response parsing error: {str(e)}")
            raise
    
    def _create_fallback_score(self, health_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a simple fallback score when Gemini analysis fails"""
        
        # Simple scoring based on available data
        scores = []
        breakdown = {}
        
        # Activity scoring
        if health_data.get('activity_data'):
            activity = health_data['activity_data']
            avg_steps = activity.get('average_steps', 0)
            if avg_steps >= 10000:
                activity_score = 90
                explanation = "Excellent daily step count exceeding 10,000 steps."
            elif avg_steps >= 8000:
                activity_score = 80
                explanation = "Good daily step count meeting health recommendations."
            elif avg_steps >= 6000:
                activity_score = 65
                explanation = "Moderate activity level with room for improvement."
            else:
                activity_score = 40
                explanation = "Low activity level, consider increasing daily movement."
            
            scores.append(activity_score)
            breakdown['activity'] = {
                'mini_score': activity_score,
                'explanation': explanation
            }
        else:
            breakdown['activity'] = {
                'mini_score': 0,
                'explanation': "No activity data available for this period."
            }
        
        # Sleep scoring
        if health_data.get('sleep_data'):
            sleep = health_data['sleep_data']
            avg_hours = sleep.get('avg_sleep_hours', 0)
            if 7 <= avg_hours <= 9:
                sleep_score = 85
                explanation = "Sleep duration is within optimal range for health and recovery."
            elif 6 <= avg_hours < 7:
                sleep_score = 70
                explanation = "Sleep duration is slightly below optimal but adequate."
            else:
                sleep_score = 50
                explanation = "Sleep duration needs improvement for better health outcomes."
            
            scores.append(sleep_score)
            breakdown['sleep_quality'] = {
                'mini_score': sleep_score,
                'explanation': explanation
            }
        else:
            breakdown['sleep_quality'] = {
                'mini_score': 0,
                'explanation': "No sleep data available for this period."
            }
        
        # Add other metric placeholders
        for metric in ['heart_health', 'body_metrics', 'exercise', 'health_metrics']:
            if metric not in breakdown:
                breakdown[metric] = {
                    'mini_score': 0,
                    'explanation': "No data available for this metric."
                }
        
        # Calculate overall score
        overall_score = int(sum(scores) / len(scores)) if scores else 50
        
        return {
            'overall_score': overall_score,
            'data_completeness_percentage': (len(scores) / 6) * 100,
            'breakdown': breakdown,
            'scoring_note': f"Fallback scoring based on {len(scores)} available metrics.",
            'key_strengths': ["Basic health metrics analyzed"],
            'improvement_areas': ["Enable more data syncing for comprehensive analysis"],
            'analyzed_at': datetime.now().isoformat(),
            'fallback_mode': True
        }
    
    def validate_health_data(self, health_data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Validate that health data has sufficient information for scoring
        
        Returns:
            Tuple of (is_valid, reason)
        """
        if not health_data:
            return False, "No health data provided"
        
        # Count available metric categories
        available_count = 0
        for key in ['activity_data', 'heart_data', 'sleep_data', 'body_data', 'exercise_data', 'health_metrics']:
            if health_data.get(key):
                available_count += 1
        
        if available_count < 2:
            return False, f"Need at least 2 types of health data for scoring. Only {available_count} available."
        
        return True, f"Sufficient data available ({available_count} out of 6 categories)"


def get_health_analyzer() -> GeminiHealthAnalyzer:
    """Factory function to get a configured health analyzer instance"""
    return GeminiHealthAnalyzer()