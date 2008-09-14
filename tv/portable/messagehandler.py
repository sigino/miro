# Miro - an RSS based video player application
# Copyright (C) 2005-2008 Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

"""messagehandler.py -- Backend message handler"""

import logging

from miro import app
from miro import database
from miro import eventloop
from miro import feed
from miro import guide
from miro import httpclient
from miro import indexes
from miro import messages
from miro import singleclick
from miro import subscription
from miro import views
from miro import opml
from miro import searchengines
from miro.feed import Feed, get_feed_by_url
from miro.playlist import SavedPlaylist
from miro.folder import FolderBase, ChannelFolder, PlaylistFolder
from miro.util import getSingletonDDBObject
from miro.xhtmltools import urlencode

import shutil

class ViewTracker(object):
    """Handles tracking views for TrackGuides, TrackChannels, TrackPlaylist and TrackItems."""

    def __init__(self):
        self.add_callbacks()
        self.reset_changes()

    def reset_changes(self):
        self.changed = set()
        self.removed = set()
        self.added =  []
        # Need to use a list because added messages must be sent in the same
        # order they were receieved
        self.changes_pending = False

    def send_messages(self):
        # Try to reduce the number of messages we're sending out.
        self.changed -= self.removed
        self.changed -= set(self.added)

        for i in reversed(xrange(len(self.added))):
            if self.added[i] in self.removed:
                # Object was removed before we sent the add message, just
                # don't send any message
                self.removed.remove(self.added.pop(i))
        message = self.make_changed_message(
                [self.InfoClass(obj) for obj in self.added],
                [self.InfoClass(obj) for obj in self.changed],
                [obj.id for obj in self.removed])
        message.send_to_frontend()
        self.reset_changes()

    def schedule_send_messages(self):
        # We don't send messages immediately so that if an object gets changed
        # multiple times, only one callback gets sent.
        if not self.changes_pending:
            eventloop.addUrgentCall(self.send_messages, 'view tracker update' )
            self.changes_pending = True

    def add_callbacks(self):
        for view in self.get_object_views():
            view.addAddCallback(self.on_object_added)
            view.addRemoveCallback(self.on_object_removed)
            view.add_change_callback(self.on_object_changed)

    def remove_callbacks(self):
        for view in self.get_object_views():
            view.removeAddCallback(self.on_object_added)
            view.removeRemoveCallback(self.on_object_removed)
            view.remove_change_callback(self.on_object_changed)

    def on_object_added(self, obj, id):
        if obj in self.removed:
            # object was already removed, we need to send that message out
            # before we send the add message.
            self.send_messages()
        self.added.append(obj)
        self.schedule_send_messages()

    def on_object_removed(self, obj, id):
        self.removed.add(obj)
        self.schedule_send_messages()

    def on_object_changed(self, obj, id):
        self.changed.add(obj)
        self.schedule_send_messages()

    def unlink(self):
        self.remove_callbacks()

class TabTracker(ViewTracker):
    def make_changed_message(self, added, changed, removed):
        return messages.TabsChanged(self.type, added, changed, removed)

    def add_callbacks(self):
        for view in self.get_object_views():
            view.addAddCallback(self.on_object_added)
            view.addRemoveCallback(self.on_object_removed)
            view.add_change_callback(self.on_object_changed)

    def remove_callbacks(self):
        for view in self.get_object_views():
            view.removeAddCallback(self.on_object_added)
            view.removeRemoveCallback(self.on_object_removed)
            view.remove_change_callback(self.on_object_changed)

    def send_initial_list(self):
        response = messages.TabList(self.type)
        current_folder_id = None
        for tab in self.get_tab_view():
            info = self.InfoClass(tab.obj)
            if tab.obj.getFolder() is None:
                response.append(info)
                if isinstance(tab.obj, FolderBase):
                    current_folder_id = tab.objID()
                    if tab.obj.getExpanded():
                        response.expand_folder(tab.objID())
                else:
                    current_folder_id = None
            else:
                if (current_folder_id is None or
                        tab.obj.getFolder().id != current_folder_id):
                    raise AssertionError("Tab ordering is wrong")
                response.append_child(current_folder_id, info)
        response.send_to_frontend()

class ChannelTracker(TabTracker):
    type = 'feed'
    InfoClass = messages.ChannelInfo

    def get_object_views(self):
        return views.visibleFeeds, views.channelFolders

    def get_tab_view(self):
        return getSingletonDDBObject(views.channelTabOrder).getView()

class PlaylistTracker(TabTracker):
    type = 'playlist'
    InfoClass = messages.PlaylistInfo

    def get_object_views(self):
        return views.playlists, views.playlistFolders

    def get_tab_view(self):
        return getSingletonDDBObject(views.playlistTabOrder).getView()

class GuideTracker(ViewTracker):
    InfoClass = messages.GuideInfo

    def get_object_views(self):
        return [views.guides]

    def make_changed_message(self, added, changed, removed):
        return messages.TabsChanged('guide', added, changed, removed)

    def send_initial_list(self):
        info_list = [messages.GuideInfo(g) for g in views.guides]
        messages.GuideList(info_list).send_to_frontend()

class ItemTrackerBase(ViewTracker):
    InfoClass = messages.ItemInfo

    def make_changed_message(self, added, changed, removed):
        return messages.ItemsChanged(self.type, self.id,
                added, changed, removed)

    def get_object_views(self):
        return [self.view]

    def send_initial_list(self):
        messages.ItemList(self.type, self.id, self.view).send_to_frontend()

class FeedItemTracker(ItemTrackerBase):
    type = 'feed'
    def __init__(self, feed):
        self.view = feed.items
        self.id = feed.id
        ItemTrackerBase.__init__(self)

class FeedFolderItemTracker(ItemTrackerBase):
    type = 'feed'
    def __init__(self, folder):
        self.view = views.items.filterWithIndex(indexes.itemsByChannelFolder,
                folder)
        self.id = folder.id
        ItemTrackerBase.__init__(self)

    def unlink(self):
        ItemTrackerBase.unlink(self)
        self.view.unlink()

class PlaylistItemTracker(ItemTrackerBase):
    type = 'playlist'
    def __init__(self, playlist):
        self.view = playlist.trackedItems.view
        self.id = playlist.id
        ItemTrackerBase.__init__(self)

class DownloadingItemsTracker(ItemTrackerBase):
    type = 'downloads'
    id = None
    def __init__(self):
        self.view = views.allDownloadingItems
        ItemTrackerBase.__init__(self)

class IndividualDownloadsTracker(ItemTrackerBase):
    type = 'individual_downloads'
    id = None
    def __init__(self):
        self.view = views.manualItems
        ItemTrackerBase.__init__(self)

class NewItemsTracker(ItemTrackerBase):
    type = 'new'
    id = None
    def __init__(self):
        self.view = views.uniqueNewWatchableItems
        ItemTrackerBase.__init__(self)

class LibraryItemsTracker(ItemTrackerBase):
    type = 'library'
    id = None
    def __init__(self):
        self.view = views.uniqueWatchableItems
        ItemTrackerBase.__init__(self)

class SearchItemsTracker(ItemTrackerBase):
    type = 'search'
    id = None
    def __init__(self):
        self.view = views.searchItems
        ItemTrackerBase.__init__(self)

def make_item_tracker(message):
    if message.type == 'downloads':
        return DownloadingItemsTracker()
    elif message.type == 'individual_downloads':
        return IndividualDownloadsTracker()
    elif message.type == 'new':
        return NewItemsTracker()
    elif message.type == 'library':
        return LibraryItemsTracker()
    elif message.type == 'search':
        return SearchItemsTracker()
    elif message.type == 'feed':
        try:
            feed = views.feeds.getObjectByID(message.id)
            return FeedItemTracker(feed)
        except database.ObjectNotFoundError:
            folder = views.channelFolders.getObjectByID(message.id)
            return FeedFolderItemTracker(folder)
    elif message.type == 'playlist':
        try:
            playlist = views.playlists.getObjectByID(message.id)
            return PlaylistItemTracker(playlist)
        except database.ObjectNotFoundError:
            playlist = views.playlistFolders.getObjectByID(message.id)
            return PlaylistItemTracker(playlist)
    else:
        logging.warn("Unknown TrackItems type: %s", message.type)

class CountTracker(object):
    """Tracks downloads count or new videos count"""
    def __init__(self):
        self.view = self.get_view()
        self.view.addAddCallback(self.on_count_changed)
        self.view.addRemoveCallback(self.on_count_changed)

    def on_count_changed(self, obj, id):
        self.send_message()

    def send_message(self):
        self.make_message(len(self.view)).send_to_frontend()

    def stop_tracking(self):
        self.view.removeAddCallback(self.on_count_changed)
        self.view.removeRemoveCallback(self.on_count_changed)

class DownloadCountTracker(CountTracker):
    def get_view(self):
        return views.downloadingItems

    def make_message(self, count):
        return messages.DownloadCountChanged(count)

class NewCountTracker(CountTracker):
    def get_view(self):
        return views.uniqueNewWatchableItems

    def make_message(self, count):
        return messages.NewCountChanged(count)

class BackendMessageHandler(messages.MessageHandler):
    def __init__(self):
        messages.MessageHandler.__init__(self)
        self.channel_tracker = None
        self.playlist_tracker = None
        self.guide_tracker = None
        self.download_count_tracker = None
        self.new_count_tracker = None
        self.item_trackers = {}

    def call_handler(self, method, message):
        name = 'handling backend message: %s' % message
        eventloop.addUrgentCall(method, name, args=(message,))

    def folder_view_for_type(self, type):
        if type == 'feed':
            return views.channelFolders
        elif type == 'playlist':
            return views.playlistFolders
        else:
            raise ValueError("Unknown Type: %s" % type)

    def view_for_type(self, type):
        if type == 'feed':
            return views.visibleFeeds
        elif type == 'playlist':
            return views.playlists
        elif type == 'feed-folder':
            return views.channelFolders
        elif type == 'playlist-folder':
            return views.playlistFolders
        elif type == 'site':
            return views.guides
        else:
            raise ValueError("Unknown Type: %s" % type)

    def handle_query_search_info(self, message):
        search_feed = app.controller.get_global_feed('dtv:search')
        messages.CurrentSearchInfo(search_feed.lastEngine,
                search_feed.lastQuery).send_to_frontend()

    def handle_track_channels(self, message):
        if not self.channel_tracker:
            self.channel_tracker = ChannelTracker()
        self.channel_tracker.send_initial_list()

    def handle_stop_tracking_channels(self, message):
        if self.channel_tracker:
            self.channel_tracker.unlink()
            self.channel_tracker = None

    def handle_track_guides(self, message):
        if not self.guide_tracker:
            self.guide_tracker = GuideTracker()
        self.guide_tracker.send_initial_list()

    def handle_stop_tracking_guides(self, message):
        if self.guide_tracker:
            self.guide_tracker.unlink()
            self.guide_tracker = None

    def handle_track_playlists(self, message):
        if not self.playlist_tracker:
            self.playlist_tracker = PlaylistTracker()
        self.playlist_tracker.send_initial_list()

    def handle_stop_tracking_playlists(self, message):
        if self.playlist_tracker:
            self.playlist_tracker.unlink()
            self.playlist_tracker = None

    def handle_mark_channel_seen(self, message):
        feed = database.defaultDatabase.getObjectByID(message.id)
        feed.markAsViewed()

    def handle_mark_item_watched(self, message):
        item = views.items.getObjectByID(message.id)
        item.markItemSeen()

    def handle_mark_item_unwatched(self, message):
        item = views.items.getObjectByID(message.id)
        item.markItemUnseen()

    def handle_set_item_resume_time(self, message):
        item = views.items.getObjectByID(message.id)
        item.setResumeTime(message.resume_time)

    def handle_set_channel_expire(self, message):
        channel_info = message.channel_info
        expire_type = message.expire_type
        expire_time = message.expire_time

        try:
            channel = views.feeds.getObjectByID(channel_info.id)
            if expire_type == "never":
                channel.setExpiration(u"never", 0)
            elif expire_type == "system":
                channel.setExpiration(u"system", expire_time)
            else:
                channel.setExpiration(u"feed", expire_time)

        except database.ObjectNotFoundError:
            logging.warning("handle_set_channel_expire: can't find channel by id %s", channel_info.id)

    def handle_set_channel_max_new(self, message):
        channel_info = message.channel_info
        value = message.max_new

        try:
            channel = views.feeds.getObjectByID(channel_info.id)
            if value == u"unlimited":
                channel.set_max_new(-1)
            else:
                channel.set_max_new(value)

        except database.ObjectNotFoundError:
            logging.warning("handle_set_channel_max_new: can't find channel by id %s", channel_info.id)

    def handle_set_channel_max_old_items(self, message):
        channel_info = message.channel_info
        max_old_items = message.max_old_items

        try:
            channel = views.feeds.getObjectByID(channel_info.id)
            channel.setMaxOldItems(max_old_items)

        except database.ObjectNotFoundError:
            logging.warning("handle_set_channel_max_new: can't find channel by id %s", channel_info.id)


    def handle_import_channels(self, message):
        opml.Importer().importSubscriptionsFrom(message.filename)

    def handle_export_channels(self, message):
        opml.Exporter().exportSubscriptionsTo(message.filename)

    def handle_rename_object(self, message):
        view = self.view_for_type(message.type)
        try:
            obj = view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("object not found (type: %s, id: %s)" %
                    (message.type, message.id))
        else:
            obj.setTitle(message.new_name)

    def handle_folder_expanded_change(self, message):
        folder_view = self.folder_view_for_type(message.type)
        try:
            folder = folder_view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("channel folder not found")
        else:
            folder.setExpanded(message.expanded)

    def handle_update_channel(self, message):
        view = views.visibleFeeds
        try:
            feed = view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("feed not found: %s" % id)
        else:
            feed.update()

    def handle_update_all_channels(self, message):
        for f in views.feeds:
            f.scheduleUpdateEvents(0)

    def handle_delete_channel(self, message):
        if message.is_folder:
            view = views.channelFolders
        else:
            view = views.visibleFeeds
        try:
            channel = view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("channel not found: %s" % id)
        else:
            if message.keep_items:
                move_to = getSingletonDDBObject(views.manualFeed)
            else:
                move_to = None
            channel.remove(move_to)

    def handle_delete_playlist(self, message):
        if message.is_folder:
            view = views.playlistFolders
        else:
            view = views.playlists
        try:
            playlist = view.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("playlist not found: %s" % message.id)
        else:
            playlist.remove()

    def handle_delete_site(self, message):
        site = views.guides.getObjectByID(message.id)
        if site.getDefault():
            raise ValueError("Can't delete default site")
        site.remove()

    def handle_tabs_reordered(self, message):
        folder_view = self.folder_view_for_type(message.type)
        if message.type == 'feed':
            item_view = views.visibleFeeds
            tab_order = getSingletonDDBObject(views.channelTabOrder)
        elif message.type == 'playlist':
            item_view = views.playlists
            tab_order = getSingletonDDBObject(views.playlistTabOrder)
        else:
            raise ValueError("Unknown Type: %s" % message.type)

        order = []
        for info in message.toplevels:
            order.append(info.id)
            if info.is_folder:
                folder = folder_view.getObjectByID(info.id)
                for child_info in message.folder_children[info.id]:
                    child_id = child_info.id
                    order.append(child_id)
                    feed = item_view.getObjectByID(child_id)
                    feed.setFolder(folder)
            else:
                feed = item_view.getObjectByID(info.id)
                feed.setFolder(None)
        tab_order.reorder(order)
        tab_order.signalChange()

    def handle_playlist_reordered(self, message):
        try:
            playlist = views.playlists.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            try:
                playlist = views.playlistFolders.getObjectByID(message.id)
            except database.ObjectNotFoundError:
                logging.warn("PlaylistReordered: Playlist not found -- %s",
                        message.id)
                return

        if set(playlist.item_ids) != set(message.item_ids):
            logging.warn("PlaylistReordered: Not all ids present in the new order\nOriginal Ids: %s\nNew ids: %s", playlist.item_ids, message.item_ids)
            return
        playlist.reorder(message.item_ids)
        playlist.signalChange()

    def handle_new_guide(self, message):
        url = message.url
        if guide.getGuideByURL(url) is None:
            guide.ChannelGuide(url, [u'*'])

    def handle_new_channel(self, message):
        url = message.url
        if not get_feed_by_url(url):
            Feed(url)
            if message.trackback:
                httpclient.grabURL(message.trackback,
                        lambda x: None, lambda x: None)

    def handle_new_channel_search_channel(self, message):
        term = message.search_term
        channel_info = message.channel_info
        location = channel_info.base_href

        if isinstance(term, unicode):
            term = term.encode("utf-8")

        if isinstance(location, unicode):
            location = location.encode("utf-8")

        if channel_info.search_term:
            term = term + " " + channel_info.search_term

        url = u"dtv:searchTerm:%s?%s" % (urlencode(location), urlencode(term))
        if not get_feed_by_url(url):
            Feed(url)

    def handle_new_channel_search_engine(self, message):
        sei = message.search_engine_info
        term = message.search_term

        title = "%s: %s" % (sei.title, term)
        url = searchengines.get_request_url(sei.name, term)

        if not url:
            return

        if not get_feed_by_url(url):
            f = Feed(url)
            f.setTitle(title)

    def handle_new_channel_search_url(self, message):
        url = message.url
        term = message.search_term

        if isinstance(term, unicode):
            term = term.encode("utf-8")

        normalized = feed.normalize_feed_url(url)

        if isinstance(url, unicode):
            url = url.encode("utf-8")

        url = u"dtv:searchTerm:%s?%s" % (urlencode(normalized), urlencode(term))
        if not get_feed_by_url(url):
            Feed(url)

    def handle_new_channel_folder(self, message):
        ChannelFolder(message.name)

    def handle_new_playlist(self, message):
        name = message.name
        ids = message.ids
        if not ids:
            ids = None
        SavedPlaylist(name, ids)

    def handle_download_url(self, message):
        singleclick.addDownload(message.url)

    def handle_check_version(self, message):
        up_to_date_callback = message.up_to_date_callback
        from miro import autoupdate
        autoupdate.check_for_updates(up_to_date_callback)

    def handle_new_playlist_folder(self, message):
        PlaylistFolder(message.name)

    def handle_add_videos_to_playlist(self, message):
        try:
            playlist = views.playlists.getObjectByID(message.playlist_id)
        except database.ObjectNotFoundError:
            logging.warn("AddVideosToPlaylist: Playlist not found -- %s",
                    message.playlist_id)
            return
        for id in message.video_ids:
            try:
                item = views.items.getObjectByID(id)
            except database.ObjectNotFoundError:
                logging.warn("AddVideosToPlaylist: Item not found -- %s", id)
                continue
            if not item.isDownloaded():
                logging.warn("AddVideosToPlaylist: Item not downloaded (%s)",
                        item)
            else:
                playlist.addItem(item)

    def handle_remove_videos_from_playlist(self, message):
        try:
            playlist = views.playlists.getObjectByID(message.playlist_id)
        except database.ObjectNotFoundError:
            logging.warn("RemoveVideosFromPlaylist: Playlist not found -- %s",
                    message.playlist_id)
            return
        to_remove = []
        for id in message.video_ids:
            if not playlist.idInPlaylist(id):
                logging.warn("RemoveVideosFromPlaylist: Id not found -- %s",
                        id)
            else:
                to_remove.append(id)
        if to_remove:
            playlist.handleRemove(to_remove)

    def handle_search(self, message):
        searchengine_id = message.id
        terms = message.terms

        search_feed = app.controller.get_global_feed('dtv:search')
        search_downloads_feed = app.controller.get_global_feed('dtv:searchDownloads')

        search_feed.lastEngine = searchengine_id
        search_feed.lastQuery = terms
        search_feed.preserveDownloads(search_downloads_feed)
        if terms:
            search_feed.lookup(searchengine_id, terms)
        else:
            search_feed.reset()

    def item_tracker_key(self, message):
        return (message.type, message.id)

    def handle_track_items(self, message):
        key = self.item_tracker_key(message)
        if key not in self.item_trackers:
            item_tracker = make_item_tracker(message)
            if item_tracker is None:
                # message type was wrong
                return
            self.item_trackers[key] = item_tracker
        else:
            item_tracker = self.item_trackers[key]
        item_tracker.send_initial_list()

    def handle_stop_tracking_items(self, message):
        key = self.item_tracker_key(message)
        try:
            item_tracker = self.item_trackers.pop(key)
        except KeyError:
            logging.warn("Item tracker not found (id: %s)", message.id)
        else:
            item_tracker.unlink()

    def handle_start_download(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("StartDownload: Item not found -- %s", message.id)
        else:
            item.download()

    def handle_cancel_download(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("CancelDownload: Item not found -- %s", message.id)
        else:
            item.expire()

    def handle_pause_all_downloads(self, message):
        for item in views.downloadingItems:
            print item
            item.pause()

    def handle_pause_download(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("PauseDownload: Item not found -- %s", message.id)
        else:
            item.pause()

    def handle_resume_all_downloads(self, message):
        for item in views.pausedItems:
            print item
            item.resume()

    def handle_resume_download(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("ResumeDownload: Item not found -- %s", message.id)
        else:
            item.resume()

    def handle_cancel_all_downloads(self, message):
        for item in views.pausedItems:
            item.expire()

        for item in views.downloadingItems:
            item.expire()

    def handle_restart_upload(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("ResumeDownload: Item not found -- %s", message.id)
        else:
            if item.downloader.getType() != 'bittorrent':
                logging.warn("%s is not a torrent", item)
            elif item.downloader.state == 'uploading':
                logging.warn("%s is currently uploading", item)
            else:
                item.startUpload()

    def handle_pause_all_uploads(self, message):
        for item in views.allDownloadingItems:
            if item.isUploading():
                item.pauseUpload()

    def handle_resume_all_uploads(self, message):
        for item in views.allDownloadingItems:
            if item.isUploadingPaused():
                item.startUpload()

    def handle_keep_video(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("KeepVideo: Item not found -- %s", message.id)
        else:
            item.save()

    def handle_save_item_as(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("SaveVideoAs: Item not found -- %s", message.id)
        else:
            logging.info("saving video %s to %s" % (item.getVideoFilename(),
                                                    message.filename))
            try:
                shutil.copyfile(item.getVideoFilename(), message.filename)
            except IOError:
                # FIXME - we should pass the error back to the frontend
                pass

    def handle_remove_video_entry(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("RemoveVideoEntry: Item not found -- %s", message.id)
        else:
            item.expire()

    def handle_delete_video(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("DeleteVideo: Item not found -- %s", message.id)
        else:
            item.deleteFiles()
            item.expire()

    def handle_rename_video(self, message):
        try:
            item = views.items.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("RenameVideo: Item not found -- %s", message.id)
        else:
            item.setTitle(message.new_name)

    def handle_autodownload_change(self, message):
        try:
            feed = views.feeds.getObjectByID(message.id)
        except database.ObjectNotFoundError:
            logging.warn("AutodownloadChange: Feed not found -- %s", message.id)
        else:
            feed.setAutoDownloadMode(message.setting)

    def handle_track_download_count(self, message):
        if self.download_count_tracker is None:
            self.download_count_tracker = DownloadCountTracker()
        self.download_count_tracker.send_message()

    def handle_stop_tracking_download_count(self, message):
        if self.download_count_tracker:
            self.download_count_tracker.stop_tracking()
            self.download_count_tracker = None

    def handle_track_new_count(self, message):
        if self.new_count_tracker is None:
            self.new_count_tracker = NewCountTracker()
        self.new_count_tracker.send_message()

    def handle_stop_tracking_new_count(self, message):
        if self.new_count_tracker:
            self.new_count_tracker.stop_tracking()
            self.new_count_tracker = None

    def handle_subscription_link_clicked(self, message):
        url = message.url
        type, subscribeURLs = subscription.findSubscribeLinks(url)
        normalizedURLs = []
        for url, additional in subscribeURLs:
            normalized = feed.normalize_feed_url(url)
            if feed.validate_feed_url(normalized):
                normalizedURLs.append((normalized, additional))
        if normalizedURLs:
            if type == 'feed':
                for url, additional in normalizedURLs:
                    feed.Feed(url)
                    if 'trackback' in additional:
                        httpclient.grabURL(additional['trackback'],
                                           lambda x: None,
                                           lambda x: None)
            elif type == 'download':
                for url, additional in normalizedURLs:
                    singleclick.addDownload(url, additional)
            elif type == 'guide':
                for url, additional in normalizedURLs:
                    if guide.getGuideByURL (url) is None:
                        guide.ChannelGuide(url, [u'*'])
            else:
                raise AssertionError("Unknown subscribe type")
