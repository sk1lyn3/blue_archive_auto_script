"""Backend gesture timing patches, kept outside task implementations."""

from core.device.backend_compat import get_backend_compat


def apply():
    from core.Baas_thread import Baas_thread

    if getattr(Baas_thread, '_swipe_compat_installed', False):
        return
    original_swipe = Baas_thread.swipe
    original_u2_swipe = Baas_thread.u2_swipe

    def profile(self):
        return get_backend_compat(self.config.control_method)

    def swipe(self, fx, fy, tx, ty, duration=None, post_sleep_time=0):
        timing = profile(self)
        if timing.swipe_min_duration:
            duration = max(timing.swipe_min_duration, duration or timing.swipe_min_duration)
        return original_swipe(self, fx, fy, tx, ty, duration, post_sleep_time)

    def u2_swipe(self, fx, fy, tx, ty, duration=None, post_sleep_time=0):
        timing = profile(self)
        if timing.u2_swipe_min_duration:
            return self.swipe(fx, fy, tx, ty,
                              max(timing.u2_swipe_min_duration, duration or timing.u2_swipe_min_duration),
                              post_sleep_time)
        return original_u2_swipe(self, fx, fy, tx, ty, duration, post_sleep_time)

    Baas_thread.swipe = swipe
    Baas_thread.u2_swipe = u2_swipe
    Baas_thread._swipe_compat_installed = True
