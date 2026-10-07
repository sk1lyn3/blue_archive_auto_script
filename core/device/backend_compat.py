"""Compatibility settings selected by the control or screenshot backend."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BackendCompat:
    swipe_min_duration: float = 0
    u2_swipe_min_duration: float = 0
    rgb_tolerance: int = 0
    cafe_zoom_delay: float = 0
    cafe_gift_shot_delay: float = 0
    cafe_student_threshold: float = 0.75


DEFAULT_COMPAT = BackendCompat()
BACKEND_COMPAT = {
    # H.264/YUV capture needs color tolerance; Companion returns PNG frames.
    "virtual_scrcpy": BackendCompat(
        swipe_min_duration=0.3, u2_swipe_min_duration=1.5, rgb_tolerance=15,
        cafe_zoom_delay=0.2, cafe_gift_shot_delay=0.6, cafe_student_threshold=0.55,
    ),
    "android_companion": BackendCompat(
        swipe_min_duration=0.3, u2_swipe_min_duration=1.5,
        cafe_zoom_delay=0.2, cafe_gift_shot_delay=0.6,
    ),
}


def get_backend_compat(method):
    return BACKEND_COMPAT.get(method, DEFAULT_COMPAT)
