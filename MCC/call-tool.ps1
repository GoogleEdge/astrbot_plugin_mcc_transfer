param(
  [Parameter(Mandatory=$true)][string]$Tool,
  [string]$Args = "{}"
)
$wd = "C:\Users\Administrator\Downloads\MCC"
$init = curl.exe -s -D "$wd\hdrT.txt" --max-time 15 -X POST "http://127.0.0.1:33333/mcp" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" --data-binary "@$wd\mcp-init.json"
$sid = (Select-String -LiteralPath "$wd\hdrT.txt" -Pattern 'Mcp-Session-Id:\s*(\S+)').Matches.Groups[1].Value.Trim()
curl.exe -s --max-time 15 -X POST "http://127.0.0.1:33333/mcp" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $sid" --data-binary "@$wd\mcp-notif.json" | Out-Null
$body = '{"jsonrpc":"2.0","id":99,"method":"tools/call","params":{"name":"' + $Tool + '","arguments":' + $Args + '}}'
$out = curl.exe -s --max-time 25 -X POST "http://127.0.0.1:33333/mcp" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $sid" --data $body
$json = $out -replace '^event: message\r?\ndata: ',''
$obj = $json | ConvertFrom-Json
$text = $obj.result.content[0].text
$parsed = $text | ConvertFrom-Json
$parsed | ConvertTo-Json -Depth 8
