import os
import datetime

root = r'D:\AII\pet-hospital-windows-amd64\windows\pet-hospital-mcp'
results = []
for dirpath, dirnames, filenames in os.walk(root):
    for f in filenames:
        fpath = os.path.join(dirpath, f)
        mtime = os.path.getmtime(fpath)
        results.append((datetime.datetime.fromtimestamp(mtime), fpath))

results.sort()
for dt, path in results:
    print(f"{dt.strftime('%Y-%m-%d %H:%M:%S')}  {path}")
