@echo off
curl -u tc:tc http://127.0.0.1:39240 -H "Content-Type: text/plain" -d "{\"jsonrpc\":\"1.0\",\"id\":\"1\",\"method\":\"importprivkey\",\"params\":[\"Y9V6meV9oknSVpk5gZm4eqRuWvmtLcn2N8xwmec3cffyAAmhDpa4\"]}"
echo.
timeout /t 2 >nul
curl -u tc:tc http://127.0.0.1:39240 -H "Content-Type: text/plain" -d "{\"jsonrpc\":\"1.0\",\"id\":\"1\",\"method\":\"getbalance\"}"
pause