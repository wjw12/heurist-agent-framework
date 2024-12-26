import json
import logging
import os
import random
import time
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional
import requests
import dotenv
import yaml
from agents.core_agent import CoreAgent
from core.llm import call_llm, LLMError
from core.imgen import generate_image_with_retry, generate_image_prompt

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dotenv.load_dotenv()

# Constants
HEURIST_BASE_URL = "https://llm-gateway.heurist.xyz/v1"
HEURIST_API_KEY = os.getenv("HEURIST_API_KEY")
FARCASTER_API_KEY = os.getenv("FARCASTER_API_KEY")
FARCASTER_SIGNER_UUID = os.getenv("FARCASTER_SIGNER_UUID")
LARGE_MODEL_ID = os.getenv("LARGE_MODEL_ID")
SMALL_MODEL_ID = os.getenv("SMALL_MODEL_ID")
CAST_WORD_LIMITS = [15, 20, 30, 35]
IMAGE_GENERATION_PROBABILITY = 0.3
CAST_HISTORY_FILE = "cast_history.json"
DRYRUN = False

if DRYRUN:
    print("DRYRUN MODE: Not posting real casts")
else:
    print("LIVE MODE: Will post real casts")

class FarcasterAPI:
    def __init__(self, api_key: str, signer_uuid: str):
        self.api_key = api_key
        self.signer_uuid = signer_uuid
        self.base_url = 'https://api.neynar.com/v2/farcaster'
        self.headers = {
            'api_key': self.api_key,
            'Content-Type': 'application/json'
        }

    def post_cast(self, message: str, image_url: Optional[str] = None) -> Optional[str]:
        """Post a cast to Farcaster, optionally with an image"""
        try:
            endpoint = f"{self.base_url}/cast"
            
            data = {
                "signer_uuid": self.signer_uuid,
                "text": message,
            }

            if image_url:
                data["embeds"] = [{"url": image_url}]

            response = requests.post(
                endpoint,
                headers=self.headers,
                json=data
            )
            
            if response.status_code == 200:
                result = response.json()
                cast_hash = result.get('cast', {}).get('hash')
                logger.info(f"Successfully posted cast with hash: {cast_hash}")
                return cast_hash
            else:
                logger.error(f"Failed to post cast. Status: {response.status_code}, Response: {response.text}")
                return None

        except Exception as e:
            logger.error(f"Error posting cast: {str(e)}")
            return None

    def get_user_casts(self, fid: str, limit: int = 20) -> Optional[Dict]:
        """Retrieve recent casts from a user"""
        try:
            response = requests.get(
                f"{self.base_url}/user/casts",
                headers=self.headers,
                params={"fid": fid, "limit": limit}
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to get user casts. Status: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Error getting user casts: {str(e)}")
            return None

class PromptConfig:
    def __init__(self, config_path: str = None):
        if config_path is None:
            project_root = Path(__file__).parent.parent
            config_path = project_root / "config" / "prompts.yaml"
        self.config_path = Path(config_path)
        self.config = self._load_config()

    def _load_config(self) -> dict:
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Error loading config: {str(e)}")
            raise

    def get_system_prompt(self) -> str:
        return self.config['system']['base']

    def get_basic_settings(self) -> list:
        return self.config['character']['basic_settings']

    def get_interaction_styles(self) -> list:
        return self.config['character']['interaction_styles']

    def get_basic_prompt_template(self) -> str:
        return self.config['templates']['basic_prompt']

    def get_cast_instruction_template(self) -> str:
        return self.config['templates']['tweet_instruction']

    def get_context_template(self) -> str:
        return self.config['templates']['context_twitter']

    def get_cast_ideas(self) -> list:
        return self.config['tweet_ideas']['options']

    def get_rules(self) -> str:
        return self.config['rules']['twitter']

class CastHistoryManager:
    def __init__(self, history_file: str = CAST_HISTORY_FILE):
        self.history_file = history_file
        self.history = self.load_history()

    def load_history(self) -> list:
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.warning(f"Error reading {self.history_file}, starting fresh")
                return []
        return []

    def add_cast(self, cast: str, metadata: Optional[Dict] = None) -> None:
        entry = {
            'timestamp': datetime.now().isoformat(),
            'cast': cast
        }
        if metadata:
            entry.update(metadata)
        
        entry = json.loads(json.dumps(entry, ensure_ascii=False))
        self.history.append(entry)
        self.save_history()

    def save_history(self) -> None:
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2)

    def get_recent_casts(self, n: int = 6) -> list:
        return [entry['cast']['cast'] for entry in self.history[-n:]]

class FarcasterAgent(CoreAgent):
    def __init__(self, core_agent=None):
        if core_agent:
            super().__setattr__('_parent', core_agent)
        else:
            super().__setattr__('_parent', self)
            super().__init__()
        
        # Initialize Farcaster specific components
        self.prompt_config = PromptConfig()
        self.history_manager = CastHistoryManager()
        self.farcaster_api = FarcasterAPI(FARCASTER_API_KEY, FARCASTER_SIGNER_UUID)
        self.register_interface('farcaster', self)

    def __getattr__(self, name):
        return getattr(self._parent, name)
        
    def __setattr__(self, name, value):
        if not hasattr(self, '_parent'):
            super().__setattr__(name, value)
        elif name == "_parent" or self is self._parent or name in self.__dict__:
            super().__setattr__(name, value)
        else:
            setattr(self._parent, name, value)

    def fill_basic_prompt(self, basic_options, style_options):
        return self.prompt_config.get_basic_prompt_template().format(
            basic_option_1=basic_options[0],
            basic_option_2=basic_options[1],
            style_option_1=style_options[0],
            style_option_2=style_options[1]
        )

    def format_cast_instruction(self, basic_options, style_options, ideas=None):
        decoration_ideas = f"Ideas: {ideas}" if ideas else "\n"
        num_words = random.choice(CAST_WORD_LIMITS)
        
        return self.prompt_config.get_cast_instruction_template().format(
            basic_option_1=basic_options[0],
            basic_option_2=basic_options[1],
            style_option_1=style_options[0],
            style_option_2=style_options[1],
            decoration_ideas=decoration_ideas,
            num_words=num_words,
            rules=self.prompt_config.get_rules()
        )

    def format_context(self, casts):
        if not casts:
            return ""
        return self.prompt_config.get_context_template().format(tweets=casts)

    async def generate_cast(self):
        """Generate a cast with improved error handling"""
        cast_data: Dict[str, Any] = {'metadata': {}}
        
        try:
            # Get recent casts for context
            past_casts = self.history_manager.get_recent_casts()
            
            # Generate randomized prompt
            basic_options = random.sample(self.prompt_config.get_basic_settings(), 2)
            style_options = random.sample(self.prompt_config.get_interaction_styles(), 2)
            cast_data['metadata'].update({
                'basic_options': basic_options,
                'style_options': style_options
            })
            
            prompt = self.fill_basic_prompt(basic_options, style_options)
            
            # Generate ideas
            instruction_cast_idea = random.choice(self.prompt_config.get_cast_ideas())
            user_prompt = (prompt + self.prompt_config.get_rules() + 
                         self.format_context(past_casts) + instruction_cast_idea)
            
            ideas = None
            cast_data['metadata']['ideas_instruction'] = instruction_cast_idea
            ideas, _ = await self.handle_message(instruction_cast_idea, source_interface='farcaster')
            cast_data['metadata']['ideas'] = ideas
            
            # Generate final cast
            user_prompt = (prompt + self.prompt_config.get_rules() + 
                         self.format_context(past_casts) + 
                         self.format_cast_instruction(basic_options, style_options, ideas))
            
            cast, _ = await self.handle_message(user_prompt, source_interface='farcaster')
            
            if not cast:
                raise Exception("Empty cast generated")
            
            # Clean and store cast
            cast = cast.replace('"', '')
            cast_data['cast'] = cast

            # Image generation
            image_url = None
            if random.random() < IMAGE_GENERATION_PROBABILITY:
                try:
                    image_prompt = await self.generate_image_prompt(cast)
                    image_url = await self.handle_image_generation(image_prompt)
                    cast_data['metadata']['image_prompt'] = image_prompt
                    cast_data['metadata']['image_url'] = image_url
                except Exception as e:
                    logger.warning(f"Failed to generate image: {str(e)}")
            
            return cast, image_url, cast_data
            
        except Exception as e:
            logger.error(f"Unexpected error in cast generation: {str(e)}")
        
        return None, None, None

    def run(self):
        """Start the Farcaster bot"""
        logger.info("Starting Farcaster bot...")
        asyncio.run(self._run())

    async def _run(self):
        while True:
            try:
                cast, image_url, cast_data = await self.generate_cast()
                
                if cast:
                    if not DRYRUN:
                        cast_hash = self.farcaster_api.post_cast(cast, image_url)
                        if cast_hash:
                            cast_data['metadata']['cast_hash'] = cast_hash
                            self.last_cast_hash = cast_hash
                            logger.info("Successfully posted cast: %s", cast)
                            for interface_name, interface in self.interfaces.items():
                                if interface_name == 'telegram':
                                    await self.send_to_interface(interface_name, {
                                        'type': 'message',
                                        'content': "Just posted a cast: " + cast_hash,
                                        'image_url': None,
                                        'source': 'farcaster',
                                        'chat_id': None
                                    })
                        else:
                            logger.error("Failed to post cast")
                    else:
                        logger.info("Generated cast: %s", cast)
                    
                    self.history_manager.add_cast(cast_data)
                    wait_time = random_interval()
                else:
                    logger.error("Failed to generate cast")
                    wait_time = 10
                
                next_time = datetime.now() + timedelta(seconds=wait_time)
                logger.info("Next cast will be posted at: %s", next_time.strftime('%H:%M:%S'))
                await asyncio.sleep(wait_time)
                
            except Exception as e:
                logger.error("Error occurred: %s", str(e))
                await asyncio.sleep(10)
                continue

def random_interval():
    """Generate a random interval between 1 and 2 hours in seconds"""
    return random.uniform(3600, 7200)

def main():
    agent = FarcasterAgent()
    agent.run()