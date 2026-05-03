"""
Health Score Cache System
Manages 24-hour caching of health scores to minimize API calls and improve performance
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
import hashlib


class HealthScoreCache:
    """Manages caching of health scores with 24-hour expiration"""
    
    def __init__(self, cache_dir: str = None):
        """
        Initialize the cache system
        
        Args:
            cache_dir: Directory to store cache files. Defaults to 'cache' in current dir
        """
        self.cache_dir = cache_dir or os.path.join(os.getcwd(), 'cache')
        
        # Create cache directory if it doesn't exist
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
    
    def _get_cache_key(self, user_id: str, days: int, date: str = None) -> str:
        """
        Generate a unique cache key for the user's health score
        
        Args:
            user_id: Unique identifier for the user
            days: Number of days analyzed (7, 30, 90)
            date: Date string (YYYY-MM-DD), defaults to today
            
        Returns:
            Unique cache key string
        """
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        # Create a unique identifier combining user, days, and date
        cache_string = f"{user_id}_{days}_{date}"
        
        # Hash for consistent filename
        cache_hash = hashlib.md5(cache_string.encode()).hexdigest()
        
        return f"health_score_{cache_hash}.json"
    
    def _get_cache_file_path(self, cache_key: str) -> str:
        """Get full path to cache file"""
        return os.path.join(self.cache_dir, cache_key)
    
    def get_cached_score(self, user_id: str, days: int = 7) -> Optional[Dict[str, Any]]:
        """
        Retrieve cached health score if valid and not expired
        
        Args:
            user_id: Unique identifier for the user
            days: Number of days analyzed
            
        Returns:
            Cached score dictionary or None if not found/expired
        """
        try:
            cache_key = self._get_cache_key(user_id, days)
            cache_file = self._get_cache_file_path(cache_key)
            
            if not os.path.exists(cache_file):
                return None
            
            # Read cache file
            with open(cache_file, 'r', encoding='utf-8') as f:
                cached_data = json.load(f)
            
            # Check if cache is expired
            if self._is_cache_expired(cached_data):
                # Remove expired cache file
                os.remove(cache_file)
                return None
            
            print(f"✅ Using cached health score for user {user_id} ({days} days)")
            return cached_data
            
        except Exception as e:
            print(f"❌ Error reading cache: {str(e)}")
            return None
    
    def store_score(self, user_id: str, score_data: Dict[str, Any], days: int = 7) -> bool:
        """
        Store health score in cache with expiration
        
        Args:
            user_id: Unique identifier for the user
            score_data: Health score data to cache
            days: Number of days analyzed
            
        Returns:
            True if successfully stored, False otherwise
        """
        try:
            cache_key = self._get_cache_key(user_id, days)
            cache_file = self._get_cache_file_path(cache_key)
            
            # Add cache metadata
            cached_data = {
                'score_data': score_data,
                'cached_at': datetime.now().isoformat(),
                'expires_at': (datetime.now() + timedelta(hours=24)).isoformat(),
                'cache_key': cache_key,
                'user_id': user_id,
                'analysis_days': days,
                'cache_version': '1.0'
            }
            
            # Write to cache file
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cached_data, f, indent=2, ensure_ascii=False)
            
            print(f"✅ Cached health score for user {user_id} (expires in 24h)")
            return True
            
        except Exception as e:
            print(f"❌ Error storing cache: {str(e)}")
            return False
    
    def _is_cache_expired(self, cached_data: Dict[str, Any]) -> bool:
        """
        Check if cached data has expired
        
        Args:
            cached_data: Cached data dictionary
            
        Returns:
            True if expired, False otherwise
        """
        try:
            expires_at_str = cached_data.get('expires_at')
            if not expires_at_str:
                return True  # No expiration time, consider expired
            
            expires_at = datetime.fromisoformat(expires_at_str)
            return datetime.now() > expires_at
            
        except Exception:
            return True  # Error parsing, consider expired
    
    def invalidate_cache(self, user_id: str, days: int = None) -> bool:
        """
        Manually invalidate cache for a user
        
        Args:
            user_id: Unique identifier for the user
            days: Specific days to invalidate, or None for all
            
        Returns:
            True if cache was invalidated
        """
        try:
            if days is not None:
                # Invalidate specific cache
                cache_key = self._get_cache_key(user_id, days)
                cache_file = self._get_cache_file_path(cache_key)
                
                if os.path.exists(cache_file):
                    os.remove(cache_file)
                    print(f"✅ Invalidated cache for user {user_id} ({days} days)")
                    return True
            else:
                # Invalidate all caches for user (scan all files)
                invalidated_count = 0
                
                for filename in os.listdir(self.cache_dir):
                    if filename.startswith('health_score_') and filename.endswith('.json'):
                        cache_file = self._get_cache_file_path(filename)
                        
                        try:
                            with open(cache_file, 'r', encoding='utf-8') as f:
                                cached_data = json.load(f)
                            
                            if cached_data.get('user_id') == user_id:
                                os.remove(cache_file)
                                invalidated_count += 1
                        except Exception:
                            continue
                
                if invalidated_count > 0:
                    print(f"✅ Invalidated {invalidated_count} cache files for user {user_id}")
                    return True
            
            return False
            
        except Exception as e:
            print(f"❌ Error invalidating cache: {str(e)}")
            return False
    
    def clean_expired_cache(self) -> int:
        """
        Clean up expired cache files
        
        Returns:
            Number of files cleaned up
        """
        cleaned_count = 0
        
        try:
            for filename in os.listdir(self.cache_dir):
                if filename.startswith('health_score_') and filename.endswith('.json'):
                    cache_file = self._get_cache_file_path(filename)
                    
                    try:
                        with open(cache_file, 'r', encoding='utf-8') as f:
                            cached_data = json.load(f)
                        
                        if self._is_cache_expired(cached_data):
                            os.remove(cache_file)
                            cleaned_count += 1
                    except Exception:
                        # If we can't read the file, consider it corrupted and remove
                        os.remove(cache_file)
                        cleaned_count += 1
            
            if cleaned_count > 0:
                print(f"✅ Cleaned up {cleaned_count} expired cache files")
            
        except Exception as e:
            print(f"❌ Error cleaning cache: {str(e)}")
        
        return cleaned_count
    
    def get_cache_info(self, user_id: str) -> Dict[str, Any]:
        """
        Get information about cached scores for a user
        
        Args:
            user_id: Unique identifier for the user
            
        Returns:
            Dictionary with cache information
        """
        cache_info = {
            'user_id': user_id,
            'cached_scores': [],
            'total_cache_files': 0
        }
        
        try:
            for filename in os.listdir(self.cache_dir):
                if filename.startswith('health_score_') and filename.endswith('.json'):
                    cache_file = self._get_cache_file_path(filename)
                    
                    try:
                        with open(cache_file, 'r', encoding='utf-8') as f:
                            cached_data = json.load(f)
                        
                        if cached_data.get('user_id') == user_id:
                            cache_info['cached_scores'].append({
                                'days': cached_data.get('analysis_days'),
                                'cached_at': cached_data.get('cached_at'),
                                'expires_at': cached_data.get('expires_at'),
                                'is_expired': self._is_cache_expired(cached_data),
                                'overall_score': cached_data.get('score_data', {}).get('overall_score')
                            })
                        
                        cache_info['total_cache_files'] += 1
                        
                    except Exception:
                        continue
        
        except Exception as e:
            cache_info['error'] = str(e)
        
        return cache_info


# Global cache instance
_cache_instance = None

def get_health_score_cache() -> HealthScoreCache:
    """
    Get a global cache instance (singleton pattern)
    
    Returns:
        HealthScoreCache instance
    """
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = HealthScoreCache()
    return _cache_instance


def get_user_id_from_session(session_data: Dict[str, Any]) -> str:
    """
    Generate a consistent user ID from session data
    
    Args:
        session_data: Flask session data
        
    Returns:
        User ID string
    """
    # Try to get user ID from profile
    if 'profile' in session_data and session_data['profile'].get('user'):
        user_data = session_data['profile']['user']
        if user_data.get('encodedId'):
            return user_data['encodedId']
    
    # Fallback to a hash of access token (less ideal but works)
    if 'access_token' in session_data:
        token_hash = hashlib.md5(session_data['access_token'].encode()).hexdigest()
        return f"user_{token_hash[:8]}"
    
    # Last resort: generate a temporary ID
    return f"temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"