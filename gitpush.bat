@echo off
set /p mensaje=Escribe el mensaje de commit: 

git add .
git commit -m "%mensaje%"
git push
pause
