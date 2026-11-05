# Local Clipper

A private, locally hosted video clipper with a dark interface inspired by the screenshots supplied with the request. It has no login, account system, paid tier, payment page, or subscription checks.

## Features

- Paste a supported public video URL and load its title, thumbnail, channel, and duration.
- Select start and end times with editable time fields and fine adjustment buttons.
- Export video as MP4 or WebM, or audio as MP3, M4A, OGG, or Opus.
- Choose a maximum video resolution (360p through 2160p, or best available).
- Use **Original quality / Fast cut** for a quick stream-copy cut when the source/container allows it. The cut can begin at the nearest keyframe.
- Use **Exact frame / Precise cut** to re-encode and cut at the selected times.
- View and download clips from **My clips**. Clips are automatically removed after 24 hours; you can also delete them manually.
- All app data and generated clips stay in this project folder on your PC.

## Windows setup

1. Install Python 3.11 or newer from <https://www.python.org/downloads/>. During installation, enable **Add Python to PATH**.
2. Extract this project folder somewhere writable, such as `Documents\LocalClipper`.
3. Double-click `start.bat`. The first run creates a virtual environment and installs Flask, yt-dlp, the YouTube JavaScript challenge component, and `static-ffmpeg` (which supplies FFmpeg and FFprobe).
4. Your browser opens at <http://127.0.0.1:5050>.
5. To stop the server, close the black terminal window or press `Ctrl+C` in it.

You need an internet connection to load and retrieve online source media, install/update packages, and fetch the FFmpeg + FFprobe binaries on first startup. The website itself runs on your PC and listens only on the local loopback address `127.0.0.1`. For full YouTube format support, a supported JavaScript runtime such as Node.js should also be installed and available on PATH.

## Updating

Run `start.bat` again. It checks and updates the Python packages before starting the app.

## Troubleshooting

- **Python was not found:** Install Python 3.11+ and enable **Add Python to PATH**, then open a new terminal and run `start.bat` again.
- **A source cannot be loaded:** The provider may have changed its site, may not expose a public downloadable stream, or may require access the local app does not have. Try a different publicly accessible URL and update the app packages.
- **The page shows a JavaScript `textContent` error or the Download clip button stays disabled:** Close the browser tab, stop the server with `Ctrl+C`, run `start.bat` again, and refresh the page. This version fixes the missing progress-icon reference and forces the browser to load the updated JavaScript.
- **FFmpeg or conversion error:** Run `start.bat` again to reinstall/update dependencies. The first startup needs internet access to fetch the FFmpeg + FFprobe build if it is not already cached. If the error continues, try MP4 or MP3 and the Precise cut option.
- **A selected quality is unavailable:** Pick a lower resolution or **Best available**. The app cannot create detail above the source's actual quality.
- **Browser says the site cannot be reached:** Check that the black terminal window is still open and that it reports `http://127.0.0.1:5050`.

## Responsible use

Use this app only for media you created, have permission to download, or are otherwise allowed to save. Follow the source platform's terms and applicable copyright rules. The app does not provide login-cookie import, DRM removal, or access to private videos.

## Project layout

```text
local_clipper/
├── app.py
├── requirements.txt
├── start.bat
├── README.md
├── templates/
│   └── index.html
├── static/
│   ├── css/style.css
│   └── js/app.js
└── data/
    ├── clips/       # Created clips
    ├── temp/        # Temporary source files while clipping
    └── clips.json   # Clip library metadata
```
