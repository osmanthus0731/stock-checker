FIX PACK — Vercel Runtime Error

This folder includes:
- vercel.json (modern schema: "functions" + "rewrites")
- vercel.legacy.json (legacy schema: "builds" + routes with "@vercel/python")
- api/index.py (imports your Flask 'app' from app.py)
- requirements.txt (correct name/deps)

How to use:
1) Put ALL files in your project ROOT (same place as app.py).
2) Try: `vercel dev`
3) If you STILL see "Function Runtimes must have a valid version", rename:
     - vercel.json -> vercel.modern.json
     - vercel.legacy.json -> vercel.json
   Then run: `vercel dev` again.

Other fixes to try (run in terminal inside project root):
- Update CLI:  npm uninstall -g vercel && npm i -g vercel@latest
- Clear link:  rmdir .vercel /s /q   (PowerShell: Remove-Item -Recurse -Force .vercel)
               vercel link   (answer '.' for directory)
- Validate JSON:  type vercel.json  (or) PowerShell: Get-Content .\vercel.json
