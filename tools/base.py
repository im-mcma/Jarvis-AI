# tools/base.py

from abc import ABC, abstractmethod
from typing import Any, Dict
from google.generativeai.types import FunctionDeclaration

class BaseTool(ABC):
    """
    کلاس پایه انتزاعی برای همه ابزارهای سفارشی.
    همه ابزارها باید از این کلاس ارث‌بری کرده و ویژگی‌ها و متدهای آن را پیاده‌سازی کنند.
    """
    name: str
    description: str
    parameters: Dict[str, Any]

    @abstractmethod
    async def execute(self, **kwargs) -> Dict:
        """
        منطق اجرایی ابزار را پیاده‌سازی می‌کند.
        این یک متد انتزاعی است و باید توسط کلاس‌های فرزند پیاده‌سازی شود.
        """
        pass

    def get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(name=self.name, description=self.description, parameters=self.parameters)
