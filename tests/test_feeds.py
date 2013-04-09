# -*- coding: utf-8 -*-
import feedparser
import json
import os

from io import StringIO

from django.core.management import call_command
from django.core.urlresolvers import reverse
from django.contrib.auth.models import User
from django.utils import timezone
from django_push.subscriber.signals import updated
from django_webtest import WebTest
from httplib import IncompleteRead
from httplib2 import Response
from mock import patch
from requests import Response as _Response, RequestException
from requests.packages.urllib3.exceptions import LocationParseError
from rq.timeouts import JobTimeoutException

from feedhq.feeds.models import Category, Feed, Entry, Favicon, UniqueFeed
from feedhq.feeds.tasks import update_feed
from feedhq.feeds.utils import FAVICON_FETCHER, USER_AGENT

from . import FeedHQTestCase as TestCase
from .factories import UserFactory, CategoryFactory, FeedFactory

TEST_DATA = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'data')


def test_file(name):
    return os.path.join(TEST_DATA, name)


def responses(code, path=None, redirection=None,
              headers={'Content-Type': 'text/xml'}):
    response = _Response()
    response.status_code = code
    if path is not None:
        with open(test_file(path), 'r') as f:
            response.raw = StringIO(f.read().decode('utf-8'))
    if redirection is not None:
        temp = _Response()
        temp.status_code = 301 if 'permanent' in redirection else 302
        temp.url = path
        response.history.append(temp)
        response.url = redirection
    response.headers = headers
    return response


class WebBaseTests(WebTest):
    @patch('requests.get')
    def test_welcome_page(self, get):
        get.return_value = responses(304)

        self.user = User.objects.create_user('testuser',
                                             'foo@example.com',
                                             'pass')
        user = UserFactory.create()
        url = reverse('feeds:home')
        response = self.app.get(url, user=user.username)
        self.assertContains(response, 'Getting started')
        FeedFactory.create(category__user=user)
        response = self.app.get(url)
        self.assertNotContains(response, 'Getting started')

    def test_bookmarklet(self):
        url = reverse('feeds:bookmarklet')
        response = self.app.get(url)
        self.assertContains(response, 'Subscribe on FeedHQ')

    def test_login_required(self):
        url = reverse('feeds:home')
        response = self.app.get(url, headers={'Accept': 'text/*'})
        self.assertEqual(response.status_code, 200)

    def test_homepage(self):
        """The homepage from a logged in user"""
        user = UserFactory.create()
        response = self.app.get(reverse('feeds:home'), user=user.username)
        self.assertContains(response, 'Home')
        self.assertContains(response, user.username)

    def test_unauth_homepage(self):
        """The home page from a logged-out user"""
        response = self.app.get(reverse('feeds:home'))
        self.assertContains(response, 'Sign in')  # login required

    def test_paginator(self):
        user = UserFactory.create()
        response = self.app.get(reverse('feeds:home', args=[5]),
                                user=user.username)
        self.assertContains(response, 'Home')

    def test_category(self):
        user = UserFactory.create()
        CategoryFactory.create(user=user, name=u'Cat yo')
        url = reverse('feeds:category', args=['cat-yo'])
        response = self.app.get(url, user=user.username)
        self.assertContains(response, 'Cat yo')

    @patch("requests.get")
    def test_only_unread(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        category = CategoryFactory.create(user=user)
        FeedFactory.create(category=category)
        url = reverse('feeds:unread_category', args=[category.slug])
        response = self.app.get(url, user=user.username)

        self.assertContains(response, category.name)
        self.assertContains(response, 'all <span class="ct">')

    def test_add_category(self):
        user = UserFactory.create()
        url = reverse('feeds:add_category')
        response = self.app.get(url, user=user.username)

        form = response.forms['category']
        response = form.submit()
        self.assertContains(response, 'errorlist')

        form['name'] = 'New Name'
        form['color'] = 'red'
        response = form.submit()
        self.assertRedirects(response, '/category/new-name/')

        # Adding a category with the same name. The slug will be different
        response = form.submit()
        self.assertRedirects(response, '/category/new-name-1/')

        # Now we add a category named 'add', which is a conflicting URL
        form['name'] = 'add'
        response = form.submit()
        self.assertRedirects(response, '/category/add-1/')

        # Add a category with non-ASCII names, slugify should cope
        form['name'] = u'北京'
        response = form.submit()
        self.assertRedirects(response, '/category/unknown/')
        response = form.submit()
        self.assertRedirects(response, '/category/unknown-1/')
        response = form.submit()
        self.assertRedirects(response, '/category/unknown-2/')

    def test_delete_category(self):
        user = UserFactory.create()
        category = CategoryFactory.create(user=user)
        url = reverse('feeds:delete_category', args=[category.slug])
        response = self.app.get(url, user=user.username)
        self.assertEqual(response.status_code, 200)

        self.assertEqual(Category.objects.count(), 1)
        form = response.forms['delete']
        response = form.submit()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Category.objects.count(), 0)

    @patch("requests.get")
    def test_feed(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        feed = FeedFactory.create(category__user=user)
        url = reverse('feeds:feed', args=[feed.pk])
        response = self.app.get(url, user=user.username)

        expected = (
            '<a href="{0}unread/">unread <span class="ct">0</span></a>'
        ).format(feed.get_absolute_url())
        self.assertContains(response, expected)

    def test_edit_category(self):
        user = UserFactory.create()
        category = CategoryFactory.create(user=user)
        url = reverse('feeds:edit_category', args=[category.slug])
        response = self.app.get(url, user=user.username)
        self.assertContains(response, 'Edit {0}'.format(category.name))

        form = response.forms['category']
        form['name'] = 'New Name'
        form['color'] = 'blue'

        response = form.submit()
        self.assertContains(response,
                            'New Name has been successfully updated')


class UpdateTests(TestCase):
    @patch("requests.get")
    def test_parse_error(self, get):
        get.side_effect = LocationParseError("Failed to parse url")
        FeedFactory.create()
        unique = UniqueFeed.objects.get()
        self.assertTrue(unique.muted)
        self.assertEqual(unique.error, UniqueFeed.PARSE_ERROR)

    @patch("requests.get")
    def test_incomplete_read(self, get):
        get.side_effect = IncompleteRead("0 bytes read")
        FeedFactory.create()
        f = UniqueFeed.objects.get()
        self.assertFalse(f.muted)
        self.assertEqual(f.error, f.CONNECTION_ERROR)

    @patch('requests.get')
    def test_ctype(self, get):
        # Updatefeed doesn't fail if content-type is missing
        get.return_value = responses(200, 'sw-all.xml', headers={})
        feed = FeedFactory.create()
        update_feed(feed.url)
        get.assert_called_with(
            feed.url,
            headers={'User-Agent': USER_AGENT % '1 subscriber',
                     'Accept': feedparser.ACCEPT_HEADER}, timeout=10)

        get.return_value = responses(200, 'sw-all.xml',
                                     headers={'Content-Type': None})
        update_feed(feed.url)
        get.assert_called_with(
            feed.url,
            headers={'User-Agent': USER_AGENT % '1 subscriber',
                     'Accept': feedparser.ACCEPT_HEADER}, timeout=10)

    @patch('requests.get')
    def test_permanent_redirects(self, get):
        """Updating the feed if there's a permanent redirect"""
        get.return_value = responses(
            301, redirection='permanent-atom10.xml',
            headers={'Content-Type': 'application/rss+xml'})
        feed = FeedFactory.create()
        feed = Feed.objects.get(pk=feed.id)
        self.assertEqual(feed.url, 'permanent-atom10.xml')

    @patch('requests.get')
    def test_temporary_redirect(self, get):
        """Don't update the feed if the redirect is not 301"""
        get.return_value = responses(
            302, redirection='atom10.xml',
            headers={'Content-Type': 'application/rss+xml'})
        feed = FeedFactory.create()
        get.assert_called_with(
            feed.url, timeout=10,
            headers={'User-Agent': USER_AGENT % '1 subscriber',
                     'Accept': feedparser.ACCEPT_HEADER},
        )
        feed = Feed.objects.get(pk=feed.id)
        self.assertNotEqual(feed.url, 'atom10.xml')

    @patch('requests.get')
    def test_content_handling(self, get):
        """The content section overrides the subtitle section"""
        get.return_value = responses(200, 'atom10.xml')
        FeedFactory.create(name='Content', url='atom10.xml')
        entry = Entry.objects.get()
        self.assertEqual(entry.sanitized_content(),
                         "<div>Watch out for <span> nasty tricks</span></div>")

    @patch('requests.get')
    def test_gone(self, get):
        """Muting the feed if the status code is 410"""
        get.return_value = responses(410)
        FeedFactory.create(url='gone.xml')
        feed = UniqueFeed.objects.get(url='gone.xml')
        self.assertTrue(feed.muted)

    @patch('requests.get')
    def test_errors(self, get):
        get.return_value = responses(304)
        feed = FeedFactory.create()

        for code in [400, 401, 403, 404, 500, 502, 503]:
            get.return_value = responses(code)
            feed = UniqueFeed.objects.get(url=feed.url)
            self.assertFalse(feed.muted)
            self.assertEqual(feed.error, None)
            self.assertEqual(feed.backoff_factor, 1)

            update_feed(feed.url, backoff_factor=feed.backoff_factor)

            feed = UniqueFeed.objects.get(url=feed.url)
            self.assertFalse(feed.muted)
            self.assertEqual(feed.error, str(code))
            self.assertEqual(feed.backoff_factor, 2)

            # Restore status for next iteration
            feed.backoff_factor = 1
            feed.error = None
            feed.save(update_fields=['backoff_factor', 'error'])

    @patch('requests.get')
    def test_backoff(self, get):
        get.return_value = responses(304)
        feed = FeedFactory.create()
        feed = UniqueFeed.objects.get(url=feed.url)
        self.assertEqual(feed.error, None)
        self.assertEqual(feed.backoff_factor, 1)

        get.return_value = responses(502)
        for i in range(12):
            update_feed(feed.url, backoff_factor=feed.backoff_factor)
            feed = UniqueFeed.objects.get(url=feed.url)
            self.assertFalse(feed.muted)
            self.assertEqual(feed.error, '502')
            self.assertEqual(feed.backoff_factor, min(i + 2, 10))

        get.side_effect = RequestException
        feed = UniqueFeed.objects.get()
        feed.error = None
        feed.backoff_factor = 1
        feed.save()
        for i in range(12):
            update_feed(feed.url, backoff_factor=feed.backoff_factor)
            feed = UniqueFeed.objects.get(url=feed.url)
            self.assertFalse(feed.muted)
            self.assertEqual(feed.error, 'timeout')
            self.assertEqual(feed.backoff_factor, min(i + 2, 10))

    @patch("requests.get")
    def test_etag_modified(self, get):
        get.return_value = responses(304)
        feed = FeedFactory.create()
        update_feed(feed.url, etag='etag', last_modified='1234', subscribers=2)
        get.assert_called_with(
            feed.url,
            headers={
                'User-Agent': USER_AGENT % '2 subscribers',
                'Accept': feedparser.ACCEPT_HEADER,
                'If-None-Match': 'etag',
                'If-Modified-Since': '1234',
            }, timeout=10)

    @patch("requests.get")
    def test_restore_backoff(self, get):
        get.return_value = responses(304)
        FeedFactory.create()
        feed = UniqueFeed.objects.get()
        feed.error = 'timeout'
        feed.backoff_factor = 5
        feed.save()
        update_feed(feed.url, error=feed.error,
                    backoff_factor=feed.backoff_factor)

        feed = UniqueFeed.objects.get()
        self.assertEqual(feed.backoff_factor, 1)
        self.assertEqual(feed.error, '')

    @patch('requests.get')
    def test_no_date_and_304(self, get):
        """If the feed does not have a date, we'll have to find one.
        Also, since we update it twice, the 2nd time it's a 304 response."""
        get.return_value = responses(200, 'no-date.xml')
        feed = FeedFactory.create()

        # Update the feed twice and make sure we don't index the content twice
        update_feed(feed.url)
        feed1 = Feed.objects.get(pk=feed.id)
        count1 = feed1.entries.count()

        update_feed(feed1.url)
        feed2 = Feed.objects.get(pk=feed1.id)
        count2 = feed2.entries.count()

        self.assertEqual(count1, count2)

    @patch("requests.get")
    def test_uniquefeed_deletion(self, get):
        get.return_value = responses(304)
        f = UniqueFeed.objects.create(url='example.com')
        self.assertEqual(UniqueFeed.objects.count(), 1)
        call_command('delete_unsubscribed')
        UniqueFeed.objects.update_feed(f.url)
        self.assertEqual(UniqueFeed.objects.count(), 0)

    @patch('requests.get')
    def test_no_link(self, get):
        get.return_value = responses(200, 'rss20.xml')
        feed = FeedFactory.create()
        update_feed(feed.url)
        self.assertEqual(Entry.objects.count(), 1)

        get.return_value = responses(200, 'no-link.xml')
        feed.url = 'no-link.xml'
        feed.save(update_fields=['url'])
        update_feed(feed.url)
        self.assertEqual(Entry.objects.count(), 1)

    @patch('requests.get')
    def test_task_timeout_handling(self, get):
        get.return_value = responses(304)
        feed = FeedFactory.create()
        get.side_effect = JobTimeoutException
        self.assertEqual(UniqueFeed.objects.get().backoff_factor, 1)
        update_feed(feed.url)
        self.assertEqual(UniqueFeed.objects.get().backoff_factor, 2)


class ModelTests(TestCase):
    def test_category_model(self):
        """Behaviour of the ``Category`` model"""
        cat = CategoryFactory.create(name='New Cat', slug='new-cat')

        cat_from_db = Category.objects.get(pk=cat.id)

        # __unicode__
        self.assertEqual('%s' % cat_from_db, 'New Cat')

        # get_absolute_url()
        self.assertEqual('/category/new-cat/', cat_from_db.get_absolute_url())

    @patch('requests.get')
    def test_feed_model(self, get):
        """Behaviour of the ``Feed`` model"""
        get.return_value = responses(200, 'rss20.xml')
        feed = FeedFactory.create(name='RSS test', url='rss20.xml')
        feed.save()

        feed_from_db = Feed.objects.get(pk=feed.id)

        # __unicode__
        self.assertEqual('%s' % feed_from_db, 'RSS test')

        # get_absolute_url()
        self.assertEqual('/feed/%s/' % feed.id, feed.get_absolute_url())

        # update()
        update_feed(feed.url)

        unique_feed = UniqueFeed.objects.get(url=feed.url)
        self.assertEqual(unique_feed.title, 'Sample Feed')
        self.assertEqual(unique_feed.link, 'http://example.org/')

        feed = Feed.objects.get(pk=feed.id)
        self.assertEqual(feed.entries.count(), 1)
        self.assertEqual(feed.entries.all()[0].title, 'First item title')

    @patch('requests.get')
    def test_entry_model(self, get):
        get.return_value = responses(200, 'sw-all.xml')
        feed = FeedFactory.create()
        update_feed(feed.url)
        title = 'RE2: a principled approach to regular expression matching'
        entry = Entry.objects.get(title=title)

        # __unicode__
        self.assertEqual('%s' % entry, title)

        # get_link()
        self.assertEqual(entry.get_link(), entry.link)
        # Setting permalink
        entry.permalink = 'http://example.com/some-url'
        self.assertEqual(entry.get_link(), entry.permalink)

    @patch("requests.get")
    def test_entry_model_behaviour(self, get):
        """Behaviour of the `Entry` model"""
        get.return_value = responses(304)
        feed = FeedFactory.create()
        entry = feed.entries.create(title='My title',
                                    user=feed.category.user,
                                    date=timezone.now())
        # __unicode__
        self.assertEqual('%s' % entry, 'My title')

        # get_absolute_url()
        self.assertEqual('/entries/%s/' % entry.id, entry.get_absolute_url())

    @patch("requests.get")
    def test_handle_etag(self, get):
        get.return_value = responses(200, headers={'etag': 'foo',
                                                   'last-modified': 'bar'})
        FeedFactory.create()
        feed = UniqueFeed.objects.get()
        self.assertEqual(feed.etag, 'foo')
        self.assertEqual(feed.modified, 'bar')


class TestFeeds(WebTest):
    @patch('requests.get')
    def test_add_feed(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        category = CategoryFactory.create(user=user)

        url = reverse('feeds:add_feed')
        response = self.app.get(url, user=user.username)
        self.assertContains(response, 'Add a feed')

        form = response.forms['feed']
        form['name'] = 'Lulz'
        response = form.submit()  # there is no URL / category
        self.assertContains(response, 'errorlist')

        form['name'] = 'Bobby'
        form['url'] = 'http://example.com/feed.xml'
        form['category'] = category.pk
        response = form.submit()
        self.assertRedirects(response, category.get_absolute_url())

    def test_feed_url_validation(self):
        user = UserFactory.create()
        category = CategoryFactory.create(user=user)
        url = reverse('feeds:add_feed')
        response = self.app.get(url, user=user.username)

        form = response.forms['feed']
        form['name'] = 'Test'
        form['url'] = 'ftp://example.com'
        form['category'] = category.pk

        response = form.submit()
        self.assertFormError(
            response, 'form', 'url',
            "Invalid URL scheme: 'ftp'. Only HTTP and HTTPS are supported.",
        )

        for invalid_url in ['http://localhost:8000', 'http://localhost',
                            'http://127.0.0.1']:
            form['url'] = invalid_url
            response = form.submit()
            self.assertFormError(response, 'form', 'url', "Invalid URL.")

    @patch("requests.get")
    def test_edit_feed(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        feed = FeedFactory.create(category__user=user)
        url = reverse('feeds:edit_feed', args=[feed.pk])
        response = self.app.get(url, user=user.username)
        self.assertContains(response, feed.name)

        form = response.forms['feed']

        form['name'] = 'New Name'
        form['url'] = 'http://example.com/newfeed.xml'
        response = form.submit().follow()
        self.assertContains(response, 'New Name has been successfully updated')

    @patch("requests.get")
    def test_delete_feed(self, get):
        get.return_value = responses(304)

        user = UserFactory.create()
        feed = FeedFactory.create(category__user=user)
        url = reverse('feeds:delete_feed', args=[feed.pk])
        response = self.app.get(url, user=user.username)
        self.assertContains(response, 'Delete')
        self.assertContains(response, feed.name)

        self.assertEqual(Feed.objects.count(), 1)
        response = response.forms['delete'].submit()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Feed.objects.count(), 0)
        # Redirects to home so useless to test

    @patch("requests.get")
    def test_invalid_page(self, get):
        get.return_value = responses(304)
        # We need more than 25 entries
        user = UserFactory.create()
        FeedFactory.create(category__user=user)
        url = reverse('feeds:home', args=[12000])  # that page doesn't exist
        response = self.app.get(url, user=user.username)
        self.assertContains(response, '<a href="/" class="current">')

    # This is called by other tests
    def _test_entry(self, from_url, user):
        self.assertEqual(self.app.get(from_url,
                                      user=user.username).status_code, 200)

        e = Entry.objects.get(title="jacobian's django-deployment-workshop")
        url = reverse('feeds:item', args=[e.pk])
        response = self.app.get(url, user=user.username)
        self.assertContains(response, "jacobian's django-deployment-workshop")

    @patch('requests.get')
    def test_entry(self, get):
        user = UserFactory.create()
        get.return_value = responses(200, 'sw-all.xml')
        feed = FeedFactory.create(category__user=user)

        url = reverse('feeds:home')
        self._test_entry(url, user)

        url = reverse('feeds:unread')
        self._test_entry(url, user)

        url = reverse('feeds:category', args=[feed.category.slug])
        self._test_entry(url, user)

        url = reverse('feeds:unread_category', args=[feed.category.slug])
        self._test_entry(url, user)

        url = reverse('feeds:feed', args=[feed.pk])
        self._test_entry(url, user)

        url = reverse('feeds:unread_feed', args=[feed.pk])
        self._test_entry(url, user)

    @patch('requests.get')
    def test_last_entry(self, get):
        user = UserFactory.create()
        get.return_value = responses(200, 'sw-all.xml')
        feed = FeedFactory.create(category__user=user)

        with self.assertNumQueries(3):
            update_feed(feed.url)
        self.assertEqual(Feed.objects.get().unread_count,
                         user.entries.filter(read=False).count())

        last_item = user.entries.order_by('date')[0]
        url = reverse('feeds:item', args=[last_item.pk])
        response = self.app.get(url, user=user.username)
        self.assertNotContains(response, 'Next →')

    def test_not_mocked(self):
        with self.assertRaises(ValueError):
            FeedFactory.create()

    @patch("requests.get")
    def test_img(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        feed = FeedFactory.create(category__user=user)
        entry = Entry.objects.create(
            feed=feed,
            title="Random title",
            subtitle='<img src="/favicon.png">',
            permalink='http://example.com',
            date=timezone.now(),
            user=user,
        )
        url = reverse('feeds:item', args=[entry.pk])
        response = self.app.get(url, user=user.username)
        self.assertContains(response, 'External media is hidden')
        self.assertNotContains(response, '<img src="/favicon.png">')
        self.assertEqual(Feed.objects.get(pk=feed.pk).media_safe, False)

        form = response.forms['images']
        response = form.submit(name='once')
        self.assertContains(response, 'Always display external media')
        self.assertContains(response, '<img src="/favicon.png">')
        self.assertEqual(Feed.objects.get(pk=feed.pk).media_safe, False)
        form = response.forms['images']
        response = form.submit(name='always')
        self.assertContains(response, 'Disable external media')
        self.assertContains(response, '<img src="/favicon.png">')
        self.assertEqual(Feed.objects.get(pk=feed.pk).media_safe, True)
        form = response.forms['images']
        response = form.submit(name='never')
        self.assertNotContains(response, 'Disable external media')
        self.assertEqual(Feed.objects.get(pk=feed.pk).media_safe, False)

    @patch('requests.get')
    def test_opml_import(self, get):
        user = UserFactory.create()
        url = reverse('feeds:import_feeds')
        response = self.app.get(url, user=user.username)

        get.return_value = responses(304)
        form = response.forms['import']

        with open(test_file('sample.opml'), 'r') as opml_file:
            form['file'] = 'sample.opml', opml_file.read()
        response = form.submit().follow()

        self.assertContains(response, '2 feeds have been imported')
        self.assertEqual(Category.objects.filter(name='Imported').count(), 1)

        # Re-import
        with open(test_file('sample.opml'), 'r') as opml_file:
            form['file'] = 'sample.opml', opml_file.read()
        response = form.submit().follow()
        self.assertContains(response, '0 feeds have been imported')

        # Import an invalid thing
        form['file'] = 'invalid', "foobar"
        response = form.submit()
        self.assertFormError(response, 'form', 'file', [
            "This file doesn't seem to be a valid OPML file."
        ])

        # Empty file
        form['file'] = 'name', ""
        response = form.submit()
        self.assertFormError(response, 'form', 'file', [
            "The submitted file is empty."
        ])

    @patch('requests.get')
    def test_categories_in_opml(self, get):
        user = UserFactory.create()
        url = reverse('feeds:import_feeds')
        response = self.app.get(url, user=user)
        self.assertEqual(response.status_code, 200)

        get.return_value = responses(304)

        form = response.forms["import"]

        with open(test_file('categories.opml'), 'r') as opml_file:
            form['file'] = 'categories.opml', opml_file.read()

        response = form.submit().follow()
        self.assertContains(response, '20 feeds have been imported')
        self.assertEqual(user.categories.count(), 7)
        with self.assertRaises(Category.DoesNotExist):
            user.categories.get(name='Imported')
        with self.assertRaises(Feed.DoesNotExist):
            Feed.objects.get(
                category__in=user.categories.all(),
                name='No title',
            )

        for c in Category.objects.all():
            c.get_absolute_url()

    def test_dashboard(self):
        user = UserFactory.create()
        url = reverse('feeds:dashboard')
        response = self.app.get(url, user=user.username)
        self.assertContains(response, 'Dashboard')

    @patch('requests.get')
    def test_unread_count(self, get):
        """Unread feed count everywhere"""
        user = UserFactory.create()
        url = reverse('profile')
        response = self.app.get(url, user=user.username)
        self.assertContains(
            response,
            '<a class="unread" title="Unread entries" href="/unread/">0</a>'
        )

        get.return_value = responses(200, 'sw-all.xml')
        FeedFactory.create(category__user=user)

        response = self.app.get(url, user=user.username)
        self.assertContains(
            response,
            '<a class="unread" title="Unread entries" href="/unread/">30</a>'
        )

    @patch('requests.get')
    def test_mark_as_read(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        feed = FeedFactory.create(category__user=user)
        url = reverse('feeds:unread')
        response = self.app.get(url, user=user.username)
        self.assertNotContains(response, 'Mark all as read')

        get.return_value = responses(200, 'sw-all.xml')
        update_feed(feed.url)

        response = self.app.get(url, user=user.username)
        self.assertContains(response, 'Mark all as read')

        form = response.forms['read']
        response = form.submit()
        self.assertRedirects(response, url)
        response = response.follow()
        self.assertContains(response, '30 entries have been marked as read')

    @patch('requests.get')
    @patch('oauth2.Client')
    def test_add_to_readability(self, Client, get):  # noqa
        client = Client.return_value
        r = Response({
            'status': 202,
            'reason': 'Accepted',
            'location': '/api/rest/v1/bookmarks/119',
            'x-article-location': '/api/rest/v1/articles/xj28dwkx',
        })
        value = json.dumps({'article': {'id': 'foo'}})
        client.request.return_value = [r, value]

        user = UserFactory.create(
            read_later='readability',
            read_later_credentials=json.dumps({
                'oauth_token': 'token',
                'oauth_token_secret': 'token secret',
            }),
        )

        get.return_value = responses(200, 'sw-all.xml')
        feed = FeedFactory.create(category__user=user)
        get.assert_called_with(
            feed.url,
            headers={'User-Agent': USER_AGENT % '1 subscriber',
                     'Accept': feedparser.ACCEPT_HEADER}, timeout=10)

        entry_pk = Entry.objects.all()[0].pk
        url = reverse('feeds:item', args=[entry_pk])
        response = self.app.get(url, user=user.username)
        self.assertContains(response, "Add to Readability")

        form = response.forms['read-later']
        response = form.submit()
        client.request.assert_called_with('/api/rest/v1/bookmarks/119',
                                          method='GET')
        self.assertEqual(Entry.objects.get(pk=entry_pk).read_later_url,
                         'https://www.readability.com/articles/foo')
        response = self.app.get(url, user=user.username)
        self.assertNotContains(response, "Add to Instapaper")

    @patch("requests.get")
    @patch('oauth2.Client')
    def test_add_to_instapaper(self, Client, get):  # noqa
        client = Client.return_value
        r = Response({'status': 200})
        client.request.return_value = [
            r,
            json.dumps([{'type': 'bookmark', 'bookmark_id': 12345,
                         'title': 'Some bookmark',
                         'url': 'http://example.com/some-bookmark'}])
        ]

        user = UserFactory.create(
            read_later='instapaper',
            read_later_credentials=json.dumps({
                'oauth_token': 'token',
                'oauth_token_secret': 'token secret',
            }),
        )
        get.return_value = responses(304)
        feed = FeedFactory.create(category__user=user)

        get.return_value = responses(200, 'sw-all.xml')

        update_feed(feed.url)
        get.assert_called_with(
            feed.url,
            headers={'User-Agent': USER_AGENT % '1 subscriber',
                     'Accept': feedparser.ACCEPT_HEADER}, timeout=10)

        entry_pk = Entry.objects.all()[0].pk
        url = reverse('feeds:item', args=[entry_pk])
        response = self.app.get(url, user=user.username)
        self.assertContains(response, "Add to Instapaper")

        form = response.forms['read-later']
        response = form.submit()
        body = 'url=http%3A%2F%2Fsimonwillison.net%2F2010%2FMar%2F12%2Fre2%2F'
        client.request.assert_called_with(
            'https://www.instapaper.com/api/1/bookmarks/add',
            body=body,
            method='POST',
        )
        self.assertEqual(Entry.objects.get(pk=entry_pk).read_later_url,
                         'https://www.instapaper.com/read/12345')
        response = self.app.get(url, user=user.username)
        self.assertNotContains(response, "Add to Instapaper")

    @patch('requests.get')
    @patch('requests.post')
    def test_add_to_readitlaterlist(self, post, get):
        user = UserFactory.create(
            read_later='readitlater',
            read_later_credentials=json.dumps({'username': 'foo',
                                               'password': 'bar'}),
        )

        get.return_value = responses(200, 'sw-all.xml')
        feed = FeedFactory.create(category__user=user)
        get.assert_called_with(
            feed.url,
            headers={'User-Agent': USER_AGENT % '1 subscriber',
                     'Accept': feedparser.ACCEPT_HEADER}, timeout=10)

        url = reverse('feeds:item', args=[Entry.objects.all()[0].pk])
        response = self.app.get(url, user=user.username)
        self.assertContains(response, 'Add to Read it later')
        form = response.forms['read-later']
        response = form.submit()
        # Read it Later doesn't provide the article URL so we can't display a
        # useful link
        self.assertContains(response, "added to your reading list")
        post.assert_called_with(
            'https://readitlaterlist.com/v2/add',
            data={u'username': u'foo',
                  'url': u'http://simonwillison.net/2010/Mar/12/re2/',
                  'apikey': 'test read it later API key',
                  u'password': u'bar',
                  'title': (u'RE2: a principled approach to regular '
                            u'expression matching')},
        )

    @patch('requests.get')
    def test_pubsubhubbub_handling(self, get):
        user = UserFactory.create()
        url = 'http://bruno.im/atom/tag/django-community/'
        get.return_value = responses(304)
        feed = FeedFactory.create(url=url, category__user=user)
        get.assert_called_with(
            url, headers={'User-Agent': USER_AGENT % '1 subscriber',
                          'Accept': feedparser.ACCEPT_HEADER},
            timeout=10)

        self.assertEqual(feed.entries.count(), 0)
        path = test_file('bruno.im.atom')
        parsed = feedparser.parse(path)
        updated.send(sender=None, notification=parsed)
        self.assertEqual(feed.entries.count(), 5)

        # Check content handling
        for entry in feed.entries.all():
            self.assertTrue(len(entry.subtitle) > 2400)

        # Check date handling
        self.assertEqual(feed.entries.filter(date__year=2011).count(), 3)
        self.assertEqual(feed.entries.filter(date__year=2012).count(), 2)

    @patch('requests.get')
    def test_bookmarklet_post(self, get):
        get.return_value = responses(304)

        user = UserFactory.create()
        CategoryFactory.create(user=user)

        url = '/subscribe/'  # hardcoded in the JS file
        with open(test_file('bruno-head.html'), 'r') as f:
            data = {
                'source': 'http://bruno.im/',
                'html': f.read(),
            }
            response = self.app.post(url, params=data, user=user.username)

        self.assertContains(response, 'value="http://bruno.im/atom/latest/"')
        form = response.forms['subscribe']

        self.assertEqual(Feed.objects.count(), 0)
        response = form.submit().follow()
        self.assertEqual(Feed.objects.count(), 1)

    def test_bookmarklet_parsing(self):
        user = UserFactory.create()
        url = reverse('feeds:bookmarklet_subscribe')
        for name, count in [('figaro', 15), ('github', 2), ('techcrunch', 3)]:
            with open(test_file('%s-head.html' % name), 'r') as f:
                response = self.app.post(url, {'html': f.read(),
                                               'source': 'http://t.com'},
                                         user=user.username)
            self.assertContains(response, name)
            self.assertEqual(len(response.content.split(
                '<div class="subscribe_form">'
            )), count + 1)

    def test_get_bookmarklet(self):
        url = reverse('feeds:bookmarklet_subscribe')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response['Accept'], 'POST')

    def test_bookmarklet_no_feed(self):
        user = UserFactory.create()
        url = reverse('feeds:bookmarklet_subscribe')
        response = self.app.post(url, params={
            'source': 'http://isitbeeroclock.com/',
            'html': '<link>',
        }, user=user.username)
        self.assertContains(response, 'No feed found')
        self.assertContains(response, 'Return to the site')


class FaviconTests(TestCase):
    @patch("requests.get")
    def test_declared_favicon(self, get):
        with open(test_file('bruno.im.png'), 'r') as f:
            fav = f.read()

        class Response:
            status_code = 200
            content = fav
            headers = {'foo': 'bar'}
        get.return_value = Response()
        Favicon.objects.update_favicon('http://example.com/')
        get.assert_called_with(
            'http://example.com/favicon.ico',
            headers={'User-Agent': FAVICON_FETCHER},
            timeout=10,
        )

    @patch("requests.get")
    def test_favicon_empty_document(self, get):
        class Response:
            status_code = 200
            content = '<?xml version="1.0" encoding="iso-8859-1"?>'
            headers = {}
        get.return_value = Response()
        Favicon.objects.update_favicon('http://example.com')

    @patch("requests.get")
    def test_favicon_parse_error(self, get):
        get.side_effect = LocationParseError("Failed to parse url")
        Favicon.objects.update_favicon('http://example.com')
