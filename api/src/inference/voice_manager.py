"""Voice pack management and caching."""

import os
from typing import Dict, List, Optional

import torch
from loguru import logger

from ..core import paths
from ..core.config import settings
from ..structures.model_schemas import VoiceConfig


class VoiceManager:
    """Manages voice loading and operations."""

    def __init__(self, config: Optional[VoiceConfig] = None):
        """Initialize voice manager.
        
        Args:
            config: Optional voice configuration
        """
        self._config = config or VoiceConfig()
        self._voice_cache: Dict[str, torch.Tensor] = {}

    def get_voice_path(self, voice_name: str, version: Optional[str] = None) -> Optional[str]:
        """Get path to voice file.
        
        Args:
            voice_name: Name of voice
            version: Optional version to filter by ("v0.19" or "v1.0")
            
        Returns:
            Path to voice file if exists, None otherwise
        """
        api_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        
        if version == "v1.0" or version is None:
            # Check v1.0 voices
            voice_path = os.path.join(api_dir, settings.voices_dir, "v1_0", f"{voice_name}.pt")
            if os.path.exists(voice_path):
                return voice_path
                
        if version == "v0.19" or version is None:
            # Check v0.19 voices
            voice_path = os.path.join(api_dir, settings.voices_dir, "v0_19", f"{voice_name}.pt")
            if os.path.exists(voice_path):
                return voice_path
                
        return None

    async def load_voice(self, voice_name: str, device: str = "cpu", version: Optional[str] = None) -> torch.Tensor:
        """Load voice tensor.
        
        Args:
            voice_name: Name of voice to load
            device: Device to load voice on
            
        Returns:
            Voice tensor
            
        Raises:
            RuntimeError: If voice loading fails
        """
        # Check if it's a combined voice request
        if "+" in voice_name:
            voices = [v.strip() for v in voice_name.split("+") if v.strip()]
            if len(voices) < 2:
                raise RuntimeError(f"Invalid combined voice name: {voice_name}")
                
            # Load and combine voices
            voice_tensors = []
            for voice in voices:
                try:
                    voice_tensor = await self.load_voice(voice, device)
                    voice_tensors.append(voice_tensor)
                except Exception as e:
                    raise RuntimeError(f"Failed to load base voice {voice}: {e}")
                    
            # Find maximum size
            max_size = max(tensor.size(0) for tensor in voice_tensors)
            
            # Pad tensors to match size
            padded_tensors = []
            for tensor in voice_tensors:
                if tensor.size(0) < max_size:
                    padding = torch.zeros(max_size - tensor.size(0), *tensor.size()[1:], device=tensor.device)
                    padded_tensor = torch.cat([tensor, padding], dim=0)
                    padded_tensors.append(padded_tensor)
                else:
                    padded_tensors.append(tensor)
                    
            # Stack and average
            combined = torch.mean(torch.stack(padded_tensors), dim=0)
            
            # For v1.0, attach combined voice ID
            if version == "v1.0" or (version is None and any("v1_0" in str(t.voice_id) if hasattr(t, 'voice_id') else False for t in voice_tensors)):
                combined.voice_id = voice_name
                
            return combined

        # Handle single voice with version
        voice_path = self.get_voice_path(voice_name, version)
        if not voice_path:
            available_voices = await self.list_voices(version)
            raise RuntimeError(
                f"Voice not found: {voice_name}. Available voices for version {version or 'any'}: {', '.join(sorted(available_voices))}"
            )

        # Check cache
        cache_key = f"{voice_path}_{device}"
        if self._config.use_cache and cache_key in self._voice_cache:
            return self._voice_cache[cache_key]

        # Load voice tensor
        try:
            voice = await paths.load_voice_tensor(voice_path, device=device)
            # Attach voice name as attribute for v1.0 models
            if version == "v1.0" or (version is None and "v1_0" in voice_path):
                voice.voice_id = voice_name
        except Exception as e:
            raise RuntimeError(f"Failed to load voice {voice_name}: {e}")

        # Cache if enabled
        if self._config.use_cache:
            self._manage_cache()
            self._voice_cache[cache_key] = voice
            logger.debug(f"Cached voice: {voice_name} on {device}")

        return voice

    def _manage_cache(self) -> None:
        """Manage voice cache size using simple LRU."""
        if len(self._voice_cache) >= self._config.cache_size:
            # Remove least recently used voice
            oldest = next(iter(self._voice_cache))
            del self._voice_cache[oldest]
            torch.cuda.empty_cache()  # Clean up GPU memory if needed
            logger.debug(f"Removed LRU voice from cache: {oldest}")

    async def combine_voices(self, voices: List[str], device: str = "cpu", version: Optional[str] = None) -> str:
        """Combine multiple voices into a new voice.
        
        Args:
            voices: List of voice names to combine
            device: Device to load voices on
            
        Returns:
            Name of combined voice
            
        Raises:
            ValueError: If fewer than 2 voices provided
            RuntimeError: If voice combination fails
        """
        if len(voices) < 2:
            raise ValueError("At least 2 voices are required for combination")

        # Create combined name using + as separator
        combined_name = "+".join(voices)

        # If saving is enabled, try to save the combination
        if settings.allow_local_voice_saving:
            try:
                # Load and combine voices
                combined_tensor = await self.load_voice(combined_name, device)
                
                # Save to disk in version-specific directory
                api_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                base_voices_dir = os.path.join(api_dir, settings.voices_dir)
                
                # Determine version directory
                if version == "v1.0":
                    voices_dir = os.path.join(base_voices_dir, "v1_0")
                elif version == "v0.19":
                    voices_dir = os.path.join(base_voices_dir, "v0_19")
                else:
                    # Default to v1.0 if no version specified
                    voices_dir = os.path.join(base_voices_dir, "v1_0")
                    
                os.makedirs(voices_dir, exist_ok=True)
                combined_path = os.path.join(voices_dir, f"{combined_name}.pt")
                try:
                    torch.save(combined_tensor, combined_path)
                    # Cache with path-based key
                    self._voice_cache[f"{combined_path}_{device}"] = combined_tensor
                except Exception as e:
                    raise RuntimeError(f"Failed to save combined voice: {e}")

            except Exception as e:
                logger.warning(f"Failed to save combined voice: {e}")
                # Continue without saving - will be combined on-the-fly when needed

        return combined_name

    async def list_voices(self, version: Optional[str] = None) -> List[str]:
        """List available voices.
        
        Args:
            version: Optional version to filter by ("v0.19" or "v1.0")
            
        Returns:
            List of voice names
        """
        voices = set()  # Use set to avoid duplicates
        try:
            api_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            base_voices_dir = os.path.join(api_dir, settings.voices_dir)
            
            if version == "v1.0" or version is None:
                # Check v1.0 voices
                v1_voices_dir = os.path.join(base_voices_dir, "v1_0")
                os.makedirs(v1_voices_dir, exist_ok=True)
                for entry in os.listdir(v1_voices_dir):
                    if entry.endswith(".pt"):
                        voices.add(entry[:-3])
                    
            if version == "v0.19" or version is None:
                # Check v0.19 voices
                v0_voices_dir = os.path.join(base_voices_dir, "v0_19")
                os.makedirs(v0_voices_dir, exist_ok=True)
                for entry in os.listdir(v0_voices_dir):
                    if entry.endswith(".pt"):
                        voices.add(entry[:-3])
                
        except Exception as e:
            logger.error(f"Error listing voices: {e}")
        return sorted(list(voices))

    def validate_voice(self, voice_path: str) -> bool:
        """Validate voice file.
        
        Args:
            voice_path: Path to voice file
            
        Returns:
            True if valid, False otherwise
        """
        try:
            if not os.path.exists(voice_path):
                return False
            voice = torch.load(voice_path, map_location="cpu")
            return isinstance(voice, torch.Tensor)
        except Exception:
            return False

    @property
    def cache_info(self) -> Dict[str, int]:
        """Get cache statistics.
        
        Returns:
            Dictionary with cache info
        """
        return {
            'size': len(self._voice_cache),
            'max_size': self._config.cache_size
        }


# Global singleton instance and lock
_manager_instance = None


async def get_manager(config: Optional[VoiceConfig] = None) -> VoiceManager:
    """Get global voice manager instance.
    
    Args:
        config: Optional voice configuration
        
    Returns:
        VoiceManager instance
    """
    global _manager_instance
    
    if _manager_instance is None:
        _manager_instance = VoiceManager(config)
    return _manager_instance