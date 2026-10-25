"""Locate Global's floating web-news close button in 720p coordinates."""
from functools import lru_cache
from pathlib import Path

import cv2


@lru_cache(maxsize=1)
def _templates():
    path = Path(__file__).resolve().parents[1] / "src/images/common/global_news_close.png"
    template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if template is None:
        return ()
    return tuple(cv2.resize(template, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
                 for scale in (0.8, 0.9, 1.0, 1.1, 1.2))


def find_close_button(frame, ratio):
    if frame is None:
        return None
    crop = frame[:int(120 * ratio), int(1100 * ratio):int(1280 * ratio)]
    if not crop.size:
        return None
    gray = cv2.cvtColor(cv2.resize(crop, (180, 120), interpolation=cv2.INTER_AREA),
                        cv2.COLOR_BGR2GRAY)
    best_score, position = 0.88, None
    for template in _templates():
        _, score, _, location = cv2.minMaxLoc(
            cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED))
        if score > best_score:
            best_score = score
            position = (round(1100 + location[0] + template.shape[1] / 2),
                        round(location[1] + template.shape[0] / 2))
    return position
