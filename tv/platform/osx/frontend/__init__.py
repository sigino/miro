from MainFrame import MainFrame, NullDisplay
from Application import Application
from HTMLDisplay import HTMLDisplay, getDTVAPICookie, getDTVAPIURL
from VideoDisplay import VideoDisplay, PlaybackController
from UIBackendDelegate import UIBackendDelegate
import UIStrings

from objc import nil
from AppKit import NSApplication

import app

###############################################################################

def exit(returnCode):
    NSApplication.sharedApplication().stop_(nil)

def quit():
    app.delegate.ensureDownloadDaemonIsTerminated()
    NSApplication.sharedApplication().terminate_(nil)

###############################################################################
