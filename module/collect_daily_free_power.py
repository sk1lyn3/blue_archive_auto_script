import time

from core import picture, position
from core.exception import FunctionCallTimeout, RequestHumanTakeOver
from core.image import compare_image, search_in_area
from core.picture import GAME_ONE_TIME_POP_UPS


def implement(self):
    self.to_main_page()
    to_purchase_pyroxenes_menu(self)
    if self.server == "Global":
        to_global_daily_free_product(self)
    elif self.server == "JP":
        to_jp_daily_free_product(self)
    else:
        to_purchase_type(self, "package")
    if detect_free_power_availability(self):
        collect_daily_free_power(self)
    else:
        self.logger.info("Daily Free Power Already Collected.")

    return_to_main_page(self)
    return True

def collect_daily_free_power(self):
    self.logger.info("Collect Daily Free Power.")

    free_package_position = (385, 423) if self.server in ("JP", "Global") else (385, 479)
    img_possibles = {
        "main_page_purchase-pyroxenes-menu": free_package_position,
        "main_page_purchase-pyroxenes-confirm-purchase-notice": (749, 571),
    }
    img_ends = "main_page_full-notice"
    rgb_ends = "reward_acquired"
    picture.co_detect(self, rgb_ends, None, img_ends, img_possibles, skip_first_screenshot=True)
    self.logger.info("Get 10 AP")

def return_to_main_page(self):
    back_position = (52, 38) if self.server in ("JP", "Global") else (1011, 114)
    img_possibles = {
        "main_page_purchase-pyroxenes-menu": back_position,
        "main_page_full-notice": (887, 165)
    }
    rgb_possibles = {
        "reward_acquired": (640, 180)
    }
    rgb_ends = "main_page"
    picture.co_detect(self, rgb_ends, rgb_possibles, None, img_possibles, skip_first_screenshot=True)

def to_purchase_pyroxenes_menu(self):
    img_possibles = GAME_ONE_TIME_POP_UPS[self.server]
    rgb_possibles = {
        "main_page": (982, 28)
    }
    img_ends = "main_page_purchase-pyroxenes-menu"

    picture.co_detect(self, None, rgb_possibles, img_ends, img_possibles, skip_first_screenshot=True)


def to_jp_daily_free_product(self):
    general_selected = [
        "main_page_purchase-pyroxenes-general-selected-1",
        "main_page_purchase-pyroxenes-general-selected-2",
    ]
    picture.co_detect(
        self,
        None,
        None,
        general_selected,
        {"main_page_purchase-pyroxenes-menu": (130, 318)},
        skip_first_screenshot=True,
    )
    picture.co_detect(
        self,
        None,
        None,
        "main_page_purchase-pyroxenes-daily-selected",
        {feature: (885, 100) for feature in general_selected},
        skip_first_screenshot=True,
    )


def to_global_daily_free_product(self):
    # Optional shop categories can move General up or down in the sidebar.
    area = (35, 230, 190, 340)
    selected = "main_page_purchase-pyroxenes-general-selected-2"
    unselected = "main_page_purchase-pyroxenes-general-not-selected"
    deadline = time.monotonic() + 60
    next_click = 0
    while self.flag_run:
        if time.monotonic() >= deadline:
            raise FunctionCallTimeout("Daily Free Power: General/Daily tab not selected.")
        self.update_screenshot_array()
        target = None
        if search_in_area(self, selected, area):
            if compare_image(self, "main_page_purchase-pyroxenes-daily-selected"):
                return
            target = (885, 100)
        else:
            point = search_in_area(self, unselected, area)
            if point:
                height, width = position.image_dic[self.identifier][unselected].shape[:2]
                target = (point[0] + width // 2, point[1] + height // 2)
        if target and time.monotonic() >= next_click:
            self.click(*target, wait_over=True)
            next_click = time.monotonic() + 2
        time.sleep(self.screenshot_interval)
    raise RequestHumanTakeOver("Daily Free Power navigation stopped.")


def to_purchase_type(self, tp):
    # position when " limited " exist
    p_1 = {
        "limited": (488, 186),
        "pyroxenes": (750, 186),
        "package": (1000, 186)
    }
    p_1 = p_1[tp]
    # position when " limited " not exist
    p_2 = {
        "pyroxenes": (590, 186),
        "package": (996, 186)
    }
    p_2 = p_2[tp]
    img_ends = {
        "limited": [
            "main_page_purchase-pyroxenes-limited-selected"
        ],
        "pyroxenes": [
            "main_page_purchase-pyroxenes-pyroxenes-selected-1",
            "main_page_purchase-pyroxenes-pyroxenes-selected-2"
        ],
        "package": [
            "main_page_purchase-pyroxenes-package-selected-1",
            "main_page_purchase-pyroxenes-package-selected-2",
            "main_page_purchase-pyroxenes-package-selected-3"
        ]
    }
    img_ends = img_ends[tp]
    img_possibles = {
        "main_page_purchase-pyroxenes-limited-selected": p_1,
        "main_page_purchase-pyroxenes-pyroxenes-selected-1": p_1,
        "main_page_purchase-pyroxenes-package-selected-1":p_1,
        "main_page_purchase-pyroxenes-pyroxenes-selected-2": p_2,
        "main_page_purchase-pyroxenes-package-selected-2": p_2,
        "main_page_purchase-pyroxenes-guide-selected-2": p_2
    }
    for img_end in img_ends:
        img_possibles.pop(img_end, None)
    picture.co_detect(self, None, None, img_ends, img_possibles, skip_first_screenshot=True)

def detect_free_power_availability(self):
    img_ends = [
        "main_page_purchase-pyroxenes-daily-free-purchasable",
        "main_page_purchase-pyroxenes-daily-free-non-purchasable",
    ]
    ret = picture.co_detect(self, None, None, img_ends, None, skip_first_screenshot=True)
    if ret == "main_page_purchase-pyroxenes-daily-free-purchasable":
        return True
    return False
