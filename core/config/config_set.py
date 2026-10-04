import json
import os
import re
from core.config.generated_user_config import Config
from core.config.generated_static_config import StaticConfig
from dataclasses import asdict, fields


class ConfigSet:
    static_config: StaticConfig = None

    def __init__(self, config_dir, translator=None):
        # change happens in self.config ( dataclass )
        # save change : dataclass --> dict --> json_str
        super().__init__()
        self._translator = translator
        if ConfigSet.static_config is None:
            ConfigSet._init_static_config()
        self.config: Config
        self.config_dir = None
        if os.path.exists(f'config/{config_dir}/config.json'):  # relative path
            self.config_dir = os.path.abspath(f'config/{config_dir}')
        elif os.path.exists(f'{config_dir}/config.json'):  # absolute path
            self.config_dir = config_dir
        else:
            raise FileNotFoundError(f'config/{config_dir}/config.json not found')
        self.server_mode = 'CN'
        self._init_config()
        self.inject_comp_list = []
        self.inject_config_list = []
        self.window = None
        self.main_thread = None
        self.signals = {}

    @staticmethod
    def _init_static_config():
        with open('config/static.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            ConfigSet.static_config = StaticConfig(**data)

    def _init_config(self):
        with open(os.path.join(self.config_dir, "config.json"), 'r', encoding='utf-8') as f:
            user_config = json.load(f)
        from core.config.default_config import DEFAULT_CONFIG
        default_config = json.loads(DEFAULT_CONFIG)
        config_data = {**default_config, **user_config}
        config_fields = {field.name for field in fields(Config)}
        self.config = Config(**{key: value for key, value in config_data.items() if key in config_fields})
        self.server_mode = self.get_server_mode(self.config.server)
        self._migrate_config()

    def _migrate_config(self):
        changed = False
        changed |= self._migrate_shop_list("CommonShopList", self.static_config.common_shop_price_list)
        changed |= self._migrate_shop_list(
            "TacticalChallengeShopList",
            self.static_config.tactical_challenge_shop_price_list
        )
        if changed:
            self.save()

    def _migrate_shop_list(self, config_key, price_lists):
        target_price = price_lists.get(self.server_mode)
        if target_price is None:
            return False

        buy_list = getattr(self.config, config_key)
        if len(buy_list) == len(target_price):
            return False

        source_price = self._find_shop_price_template_by_length(price_lists, len(buy_list))
        if source_price is None:
            setattr(self.config, config_key, [0] * len(target_price))
            return True

        values_by_name = {}
        for idx, item in enumerate(source_price):
            values_by_name.setdefault(item[0], []).append(buy_list[idx])

        migrated = []
        for item in target_price:
            values = values_by_name.get(item[0], [])
            migrated.append(values.pop(0) if values else 0)

        setattr(self.config, config_key, migrated)
        return True

    @staticmethod
    def _find_shop_price_template_by_length(price_lists, length):
        candidates = [price for price in price_lists.values() if len(price) == length]
        if not candidates:
            return None

        first_names = [item[0] for item in candidates[0]]
        for price in candidates[1:]:
            if [item[0] for item in price] != first_names:
                return None
        return candidates[0]

    @staticmethod
    def get_server_mode(server):
        if server in ['官服', 'B服']:
            return 'CN'
        if server in ['国际服', '国际服青少年', '韩国ONE', 'Steam国际服']:
            return 'Global'
        if server in ['日服', '日服PC端']:
            return 'JP'

    def _get_translator(self):
        if self._translator is None:
            from gui.util.translator import baasTranslator
            self._translator = baasTranslator
        return self._translator

    def get(self, key, default=None):
        self._init_config()
        value = getattr(self.config, key, default)
        return self._get_translator().tr('ConfigTranslation', value)

    def has(self, key):
        self._init_config()
        return hasattr(self.config, key)

    def set(self, key, value):
        self._init_config()
        value = self._get_translator().undo(value)
        setattr(self.config, key, value)
        self.save()
        self.dynamic_update(key)

    def update(self, key, value):
        self.set(key, value)

    def save(self):
        data = asdict(self.config)
        with open(os.path.join(self.config_dir, "config.json"), 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def dynamic_update(self, key):
        if key not in self.inject_config_list: return
        for comp in self.inject_comp_list:
            comp.config_updated(key)

    def __getitem__(self, item: str):
        return self.get(item)

    def add_signal(self, key, signal):
        self.signals[key] = signal

    def get_signal(self, key):
        return self.signals.get(key)

    def get_signals(self):
        return self.signals

    def set_window(self, window):
        self.window = window

    def get_window(self):
        return self.window

    def set_main_thread(self, thread):
        self.main_thread = thread

    def get_main_thread(self):
        return self.main_thread

    def inject(self, component, string_rule, attribute="setText"):
        """
        Inject a component with a string rule
        :param component: Component to inject
        :param string_rule: String rule
        :param attribute: Attribute to inject (default is setText)
        :return: BoundComponent, which can be ignored
        """
        from gui.util.customized_ui import BoundComponent
        bounded = BoundComponent(component, string_rule, self, attribute)
        self.inject_config_list.extend(re.findall(r'{(.*?)}', string_rule))
        self.inject_comp_list.append(bounded)
        return bounded

    def update_create_quantity_entry(self):
        dft = self.static_config.create_item_order[self.server_mode]["basic"]
        dft_list = [item for sublist in dft.values() for item in sublist]
        pop_list = []
        for key in self.config.create_item_holding_quantity:
            if key not in dft_list:
                pop_list.append(key)
        for key in pop_list:
            self.config.create_item_holding_quantity.pop(key)

        for entry in dft_list:
            if entry not in self.config.create_item_holding_quantity:
                self.config.create_item_holding_quantity[entry] = -1
        self.save()
