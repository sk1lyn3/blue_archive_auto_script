from module.activities.activity_utils import (
    activity_sweep,
    explore_activity_mission,
    explore_activity_story,
)


def sweep(self):
    return activity_sweep(self)


def explore_story(self):
    return explore_activity_story(self)


def explore_mission(self):
    return explore_activity_mission(self)
