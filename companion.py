#!/usr/bin/python3

# companion.py
# GNU GENERAL PUBLIC LICENSE, Version 3
# (c) Georg Sieber 2020
# github.com/schorschii

# this script emulates the functionality of the Atlassian Companion App (Windows/Mac) for usage on Linux clients
# please see README.md for installation instructions


from subprocess import check_output
from urllib.parse import urlparse
import asyncio
import pathlib
import pickle
import websockets
import json
import urllib.request
import subprocess
import time
import requests
import uuid
import pyinotify
import os
import hashlib
import wx

DOWNLOAD_DIR  = str(pathlib.Path.home()) + "/.cache/companion/tmp" # temp dir for downloading files
CONFIG_DIR    = str(pathlib.Path.home()) + "/.config/companion" # config/settings dir
ALLOWED_FILE  = CONFIG_DIR + "/" + "allowed" # file for allowed sites

FILES         = [] # temp storage for downloaded file metadata
ALLOWED_SITES = [] # stores allowed site names

# create directories
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

# load config
if os.path.isfile(ALLOWED_FILE):
    ALLOWED_SITES = pickle.load( open(ALLOWED_FILE, "rb") )
    print(ALLOWED_SITES)

def askAllowSite(sitename):
    global ALLOWED_SITES
    global ALLOWED_FILE
    app = wx.App()
    dlg = wx.MessageDialog(None, "Do you want to trust »"+sitename+"«?", "Allow New Confluence Site", wx.YES_NO | wx.ICON_QUESTION | wx.STAY_ON_TOP)
    dlg.Center(); result = dlg.ShowModal()
    frame = wx.Frame(None); frame.Show(); frame.Close(); app.MainLoop()
    if result == wx.ID_YES:
        ALLOWED_SITES.append(sitename) # add to allowed site array
        pickle.dump( ALLOWED_SITES, open(ALLOWED_FILE, "wb") ) # save to file
        return True; # report success
    return False;

async def handleJson(websocket, requestjson):
    global FILES
    global DOWNLOAD_DIR
    global ALLOWED_SITES
    responsejson = {}
    if(requestjson["type"] == "authentication"):
        currentSiteTitle = requestjson["payload"]["payload"]["siteTitle"]
        if(currentSiteTitle in ALLOWED_SITES or askAllowSite(currentSiteTitle)):
            print("ACCEPTED SITE: " + requestjson["payload"]["payload"]["siteTitle"])
            responsejson = {
                "requestID": requestjson["requestID"],
                "type": "authentication-status",
                "payload": "ACCEPTED"
            }
        else:
            print("REJECTED SITE: " + requestjson["payload"]["payload"]["siteTitle"])
            responsejson = {
                "requestID": requestjson["requestID"],
                "type": "authentication-status",
                "payload": "REJECTED"
            }
        await send(websocket, json.dumps(responsejson))

    elif(requestjson["type"] == "new-transaction" and requestjson["payload"]["transactionType"] == "file"):
        newUuid = str(uuid.uuid4())
        print("new uuid: "+newUuid)
        responsejson = {
            "requestID": requestjson["requestID"],
            "payload": newUuid
        }
        await send(websocket, json.dumps(responsejson))

    elif(requestjson["type"] == "list-apps"):
        responsejson = {
            "requestID": requestjson["requestID"],
            "payload": [{
                "displayName": "Linux (yay!)",
                "imageURI": "",
                "id": "2a2fe73b2ed43010dba316046ce79923",
                "windowsStore": False
            }]
        }
        await send(websocket, json.dumps(responsejson))

    elif(requestjson["type"] == "launch-file-in-app"):
        appId = requestjson["payload"]["applicationID"]
        transId = requestjson["transactionID"]
        fileUrl = requestjson["payload"]["fileURL"]
        fileName = requestjson["payload"]["fileName"]
        filePath = DOWNLOAD_DIR + "/" + fileName

        # inform confluence about that the download started
        responsejson = {
            "eventName": "file-download-start",
            "type": "event",
            "payload": appId,
            "transactionID": transId
        }
        await send(websocket, json.dumps(responsejson))

        # start download
        urllib.request.urlretrieve(fileUrl, filePath)

        # store file info for further requests (upload)
        FILES.append({"transId":transId, "fileName":fileName})

        # inform confluence that the download finished
        responsejson = {
            "eventName": "file-downloaded",
            "type": "event",
            "payload": None,
            "transactionID": transId
        }
        await send(websocket, json.dumps(responsejson))

        # set up file watcher
        wm = pyinotify.WatchManager()
        wm.add_watch(DOWNLOAD_DIR, pyinotify.IN_MODIFY | pyinotify.IN_CLOSE_WRITE)
        notifier = pyinotify.ThreadedNotifier(wm,
            FileChangedHandler( dict={
                "websocket":websocket,
                "appId":appId,
                "transId":transId,
                "filePath":filePath,
                "fileMd5":md5(filePath)
            })
        )
        notifier.start()

        # start application
        subprocess.call(["xdg-open", filePath])

    elif(requestjson["type"] == "upload-file-in-app"):
        transId = requestjson["transactionID"]
        fileUrl = requestjson["payload"]["uploadUrl"]

        # get stored file name
        fileName = None
        for f in FILES:
            if(f["transId"] == transId):
                fileName = f["fileName"]
                break
        filePath = DOWNLOAD_DIR + "/" + fileName

        # inform confluence that upload started
        responsejson = {
            "eventName": "file-direct-upload-start",
            "type": "event",
            "payload": {
                "fileID": requestjson["payload"]["fileID"],
                "directUploadId": 2
            },
            "transactionID": transId
        }
        await send(websocket, json.dumps(responsejson))

        # now upload the modified file
        print("Now uploading " + fileName + " to: " + fileUrl)
        parsed_uri = urlparse(fileUrl)
        host = '{uri.netloc}'.format(uri=parsed_uri)
        origin = '{uri.scheme}://{uri.netloc}'.format(uri=parsed_uri)
        headers = {
            "Host": host,
            "origin": origin,
            "Accept": None,
            "Accept-Language": "de",
            "X-Atlassian-Token": "nocheck"
            #"User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) AtlassianCompanion/0.6.2 Chrome/61.0.3163.100 Electron/2.1.0-unsupported.20180809 Safari/537.36"
        }
        with open(filePath, 'rb') as f:
            r = requests.post(
                fileUrl,
                files={
                    "comment": ("comment", "Uploaded by Companion for Linux (yay!)"),
                    "file": (fileName, f)
                },
                headers=headers
            )
            print(r)
            print(r.text)

        # inform confluence that upload finished
        responsejson = {
            "eventName": "file-direct-upload-progress",
            "type": "event",
            "payload": {
                "progress": { "percentage": 100 },
                "directUploadId": 2
            },
            "transactionID": transId
        }
        await send(websocket, json.dumps(responsejson))

        # inform confluence that upload finished
        responsejson = {
            "eventName": "file-direct-upload-end",
            "type": "event",
            "payload": {
                "fileID": requestjson["payload"]["fileID"],
                "directUploadId": 2
            },
            "transactionID": transId
        }
        await send(websocket, json.dumps(responsejson))

class FileChangedHandler(pyinotify.ProcessEvent):
    def my_init(self, dict):
        self._websocket = dict["websocket"]
        self._appId = dict["appId"]
        self._transId = dict["transId"]
        self._filePath = os.path.abspath(dict["filePath"])
        self._fileMd5 = dict["fileMd5"]

    # IN_CLOSE to support FreeOffice
    # (FreeOffice does not modify the file but creates a temporary file,
    # then deletes the original and renames the temp file to the original file name.
    # That's why IN_MODIFY is not called when using FreeOffice.)
    def process_IN_CLOSE_WRITE(self, event):
        self.process_IN_MODIFY(event=event)

    # IN_MODIFY to support LibreOffice
    # (LibreOffice modifies the file directly.)
    def process_IN_MODIFY(self, event):
        if(self._filePath == event.pathname):
            print("matching file event: " + event.pathname)
            newFileMd5 = md5(event.pathname)
            if(self._fileMd5 == newFileMd5):
                print("-> file content not changed, ingnoring")
            else:
                print("-> file content changed, inform confluence")
                self._fileMd5 = newFileMd5
                # inform confluence about the changes
                responsejson = {
                    "eventName": "file-change-detected",
                    "type": "event",
                    "payload": self._appId,
                    "transactionID": self._transId
                }
                self._loop = asyncio.new_event_loop()
                task = self._loop.create_task(self._websocket.send(json.dumps(responsejson)))
                self._loop.run_until_complete(task)
                print("> " + json.dumps(responsejson))

async def companionHandler(websocket, path):
    while(True):
        request = await websocket.recv()
        print(f"< {request}")
        await handleJson( websocket, json.loads(request) )

async def send(websocket, response):
    await websocket.send(response)
    print(f"> {response}")

def md5(fname):
    hash_md5 = hashlib.md5()
    with open(fname, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

#ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
#ssl_context.load_cert_chain('demo-cert/companion.crt', 'demo-cert/companion.key')

start_server = websockets.serve(
    companionHandler, "localhost", 31459#, ssl=ssl_context
)

asyncio.get_event_loop().run_until_complete(start_server)
asyncio.get_event_loop().run_forever()
