from abc import ABC, abstractmethod
from typing import Any


class ImageProvider(ABC):
    @abstractmethod
    def generate_image(
        self, *, shot_id: str, prompt: str, reference_image_paths: list[str] | None = None
    ) -> dict[str, Any]:
        """返回至少包含 filePath 的 dict。reference_image_paths 支持传多张（比如双人同框镜头）。"""


class VideoProvider(ABC):
    @abstractmethod
    def generate_video(
        self, *, shot_id: str, start_image_path: str, end_image_path: str | None, prompt: str
    ) -> dict[str, Any]:
        """返回至少包含 filePath 的 dict"""


class VoiceProvider(ABC):
    @abstractmethod
    def generate_voice(self, *, shot_id: str, text: str, reference_audio_path: str | None = None, voice_id: str | None = None, speed: float = 1.0) -> dict[str, Any]:
        """返回至少包含 filePath 的 dict"""


class ProviderRegistry:
    """
    每类能力(kind)可以注册多个具体实现(name)，运行时按配置选择。
    M0 阶段只有注册表结构；真实实现(SeedreamImageProvider / SeedanceVideoProvider /
    IndexTTSVoiceProvider)在 M1 接入。
    """

    def __init__(self) -> None:
        self._registry: dict[tuple[str, str], object] = {}

    def register(self, kind: str, name: str, impl: object) -> None:
        self._registry[(kind, name)] = impl

    def resolve(self, kind: str, name: str = "default") -> object:
        key = (kind, name)
        if key not in self._registry:
            raise KeyError(f"未注册的 provider: kind={kind} name={name}")
        return self._registry[key]


registry = ProviderRegistry()
