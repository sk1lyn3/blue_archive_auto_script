"""Select the display lifecycle backend without task-level transport branches."""

DISPLAY_METHODS = ('virtual_scrcpy', 'android_companion')


def display_backend(method):
    if method == 'android_companion':
        from core.device.android_companion.client import AndroidCompanionClient
        return AndroidCompanionClient
    if method == 'virtual_scrcpy':
        from core.device.virtual_scrcpy_client import VirtualScrcpyClient
        return VirtualScrcpyClient
    raise ValueError(f'Unsupported display backend: {method}')


def release_display_backends():
    # Do not import or initialize an unused backend during ordinary ADB/u2 runs.
    import sys
    errors = []
    for name, class_name in (
        ('core.device.android_companion.client', 'AndroidCompanionClient'),
        ('core.device.virtual_scrcpy_client', 'VirtualScrcpyClient'),
    ):
        module = sys.modules.get(name)
        if module is not None:
            try:
                getattr(module, class_name).release_all()
            except Exception as error:
                errors.append(str(error))
    if errors:
        raise RuntimeError('; '.join(errors))
