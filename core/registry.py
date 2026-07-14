"""Plugin registry: discovers and instantiates market, strategy, data feed, and risk plugins.

Each plugin type lives in its own top-level package (markets/, strategies/,
data_feeds/, risk/) as a sibling module to that package's base.py. Any concrete
(non-abstract) subclass of the package's contract class found in those modules
is auto-registered under its identifying `code` / `mode` / `name` value.
"""

import importlib
import inspect
import pkgutil
import warnings
from types import ModuleType
from typing import Dict, List, Type, TypeVar

from data_feeds.base import DataFeed
from markets.base import MarketAdapter
from risk.base import RiskEngine
from strategies.base import StrategyPlugin

T = TypeVar("T")

# Property names checked, in order, to derive a plugin's registry key.
_KEY_ATTRS = ("code", "mode", "name")


class PluginNotFoundError(KeyError):
    """Raised when a requested plugin key isn't registered."""


class PluginRegistry:
    """Discovers concrete subclasses of `base_class` inside `package_name` and
    instantiates them on demand."""

    def __init__(self, package_name: str, base_class: Type[T]):
        self.package_name = package_name
        self.base_class = base_class
        self._classes: Dict[str, Type[T]] = {}
        self._discovered = False

    def discover(self, force: bool = False) -> Dict[str, Type[T]]:
        """Import every sibling module in the package and register any concrete
        plugin classes found. Cached after the first run unless force=True."""
        if self._discovered and not force:
            return self._classes

        self._classes = {}
        package = importlib.import_module(self.package_name)

        for _, module_name, is_pkg in pkgutil.iter_modules(package.__path__):
            if is_pkg or module_name == "base":
                continue
            full_name = f"{self.package_name}.{module_name}"
            try:
                module = importlib.import_module(full_name)
            except Exception as exc:
                warnings.warn(f"Skipping plugin module '{full_name}': failed to import ({exc})")
                continue
            self._register_from_module(module)

        self._discovered = True
        return self._classes

    def _register_from_module(self, module: ModuleType) -> None:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue  # skip names imported into this module from elsewhere
            if obj is self.base_class or not issubclass(obj, self.base_class):
                continue
            if inspect.isabstract(obj):
                continue
            key = self._plugin_key(obj)
            if key in self._classes and self._classes[key] is not obj:
                warnings.warn(
                    f"Plugin key '{key}' registered by both "
                    f"'{self._classes[key].__module__}.{self._classes[key].__name__}' and "
                    f"'{obj.__module__}.{obj.__name__}'; keeping the latter."
                )
            self._classes[key] = obj

    @staticmethod
    def _plugin_key(cls: Type[T]) -> str:
        """Derive a registry key from a plugin class's `code`, `mode`, or `name`
        property. Falls back to the class name if the class can't be probed
        with a no-arg constructor."""
        try:
            instance = cls()
        except TypeError:
            return cls.__name__

        for attr in _KEY_ATTRS:
            value = getattr(instance, attr, None)
            if isinstance(value, str) and value:
                return value
        return cls.__name__

    def get_class(self, key: str) -> Type[T]:
        self.discover()
        try:
            return self._classes[key]
        except KeyError:
            raise PluginNotFoundError(
                f"No {self.base_class.__name__} plugin registered under key '{key}'. "
                f"Available: {sorted(self._classes)}"
            ) from None

    def create(self, key: str, *args, **kwargs) -> T:
        """Instantiate the plugin registered under `key`."""
        cls = self.get_class(key)
        return cls(*args, **kwargs)

    def list_available(self) -> List[str]:
        self.discover()
        return sorted(self._classes)


market_registry = PluginRegistry("markets", MarketAdapter)
strategy_registry = PluginRegistry("strategies", StrategyPlugin)
data_feed_registry = PluginRegistry("data_feeds", DataFeed)
risk_registry = PluginRegistry("risk", RiskEngine)


def get_market(code: str, *args, **kwargs) -> MarketAdapter:
    return market_registry.create(code, *args, **kwargs)


def get_strategy(code: str, *args, **kwargs) -> StrategyPlugin:
    return strategy_registry.create(code, *args, **kwargs)


def get_data_feed(mode: str, *args, **kwargs) -> DataFeed:
    return data_feed_registry.create(mode, *args, **kwargs)


def get_risk_engine(name: str, *args, **kwargs) -> RiskEngine:
    return risk_registry.create(name, *args, **kwargs)


def list_markets() -> List[str]:
    return market_registry.list_available()


def list_strategies() -> List[str]:
    return strategy_registry.list_available()


def list_data_feeds() -> List[str]:
    return data_feed_registry.list_available()


def list_risk_engines() -> List[str]:
    return risk_registry.list_available()
