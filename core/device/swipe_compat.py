"""Backend gesture timing patches, kept outside task implementations."""

# Minimum seconds for (swipe, u2_swipe). Both calls use the selected control
# backend for these displays, since uiautomator2 cannot target their display.
SWIPE_MIN_DURATIONS = {
    'virtual_scrcpy': (0.3, 1.5),
    'android_companion': (0.3, 1.5),
}


def apply():
    from core.Baas_thread import Baas_thread

    original_swipe = Baas_thread.swipe
    original_u2_swipe = Baas_thread.u2_swipe

    def profile(self):
        return SWIPE_MIN_DURATIONS.get(self.config.control_method)

    def swipe(self, fx, fy, tx, ty, duration=None, post_sleep_time=0):
        timing = profile(self)
        if timing is not None:
            duration = max(timing[0], duration or timing[0])
        return original_swipe(self, fx, fy, tx, ty, duration, post_sleep_time)

    def u2_swipe(self, fx, fy, tx, ty, duration=None, post_sleep_time=0):
        timing = profile(self)
        if timing is not None:
            return self.swipe(fx, fy, tx, ty,
                              max(timing[1], duration or timing[1]), post_sleep_time)
        return original_u2_swipe(self, fx, fy, tx, ty, duration, post_sleep_time)

    Baas_thread.swipe = swipe
    Baas_thread.u2_swipe = u2_swipe
