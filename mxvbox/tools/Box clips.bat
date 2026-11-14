@echo off
rem Drag video files onto this to ADD them to the MXV box's clip library on the Pi.
rem The box picks new clips up in the background while it plays. Needs ffmpeg + the Pi.
py -3 "%~dp0make_clips.py" %*
echo.
pause
