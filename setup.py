from setuptools import setup

setup(
    app=['app.py'],
    data_files=[],
    options={
        'py2app': {
            'packages': ['rumps', 'requests'],
            'includes': ['Foundation', 'AppKit', 'PyObjCTools.AppHelper'],
            'plist': {
                'LSUIElement': True,
                'CFBundleName': 'KimiMonitor',
                'CFBundleShortVersionString': '1.0',
            },
        }
    },
    setup_requires=['py2app'],
)
