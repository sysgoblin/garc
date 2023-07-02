import os
import re
import sys
import logging
import time
import requests
import datetime
from bs4 import BeautifulSoup
import html
import configparser
from typing import Dict

get_input = input
str_type = str


class Garc(object):
    """
    Garc allows you retrieve data from the Gab API.
    """

    def __init__(
        self,
        user_account=None,
        user_password=None,
        connection_errors=0,
        http_errors=0,
        profile="main",
        config=None,
    ):
        """
        Create a Garc instance. If account informaton isn't given it will search for them.
        """

        self.user_account = user_account
        self.user_password = user_password
        self.connection_errors = connection_errors
        self.http_errors = http_errors
        self.cookie = None
        self.profile = profile
        self.search_types = ["status", "top", "account", "group", "link", "feed", "hashtag"]

        if config:
            self.config = config
        else:
            self.config = self.default_config()

        self.check_keys()
        self.load_headers()

    def search(
            self,
            q: str,
            type: str="status",
            gabs: int=-1,
            only_verified: bool=False,
            exact: bool=False,
        ) -> Dict:
        """Search Gab using provided query.

        Args:
            q (int): Query term.
            type (str, optional): Search type to use. Defaults to "status".
            gabs (int, optional): Number of gabs to return. Defaults to as many as possible.
            only_verified (bool, optional): Only return gabs from verified accounts. Defaults to False.
            exact (bool, optional): Results should match the exact query string. Defaults to False.

        Raises:
            ValueError: If invalid search type is provided.

        Yields:
            dict: JSON response from Gab.
        """

        # validate search type
        if type not in self.search_types:
            raise ValueError(
                f"Invalid search type. Please use one of the following: {', '.join(self.search_types)}"
            )

        # if gabs is -1, we want to retrieve as many gabs as possible
        if gabs == -1:
            pages_count = 100000000 # set to a large number
        else:
            # pages return 25 gabs per page so we need to divide by 25
            # and round up to get the number of pages we need to retrieve
            pages_count = int(gabs / 25) + (gabs % 25 > 0)

        # if exact, we want to wrap the query in quotes
        if exact:
            q = f'"{q}"'

        num_gabs = 0
        for page in range(pages_count):
            url = f"https://gab.com/api/v3/search?type=status&onlyVerified={only_verified}&q={q}&resolve=true&page={page}"
            resp = self.get(url)

            if resp.status_code == 500:
                logging.error("search for %s failed, recieved 500 from Gab.com", (q))
                break
            elif resp.status_code == 429:
                logging.warn("rate limited, sleeping two minutes")
                time.sleep(100)
                continue

            json_response = resp.json()
            # if there are no keys in the response, there are no results
            if not json_response.keys():
                break
            # no matter the search type, the first key in the response contains the posts
            # so we need to get the first key and then iterate over the posts
            first_key = list(json_response.keys())[0]
            posts = json_response[first_key]

            if not posts:
                logging.info("No more posts returned for search: %s", (q))
                break
            for post in posts:
                num_gabs += 1
                yield post
                if num_gabs == gabs:
                    return

    def hashtag(self, q, gabs=-1):
        """
        Pass in a hashtag. Defaults to recent sort by date.
        Defaults to retrieving as many historical gabs as possible.
        """

        num_gabs = 0
        max_id = ""
        while True:
            url = "https://gab.com/api/v1/timelines/tag/%s?max_id=%s" % (q, max_id)
            resp = self.get(url)

            # We should probably implement some better error catching
            # not simply checking for a 500 to know we've gotten all the gabs possible
            if resp.status_code == 500:
                logging.error("search for %s failed, recieved 500 from Gab.com", (q))
                break
            elif resp.status_code == 429:
                logging.warn("rate limited, sleeping two minutes")
                time.sleep(100)
                continue
            posts = resp.json()

            # API seems to be more stable than previously and will not send 500
            # as it runs out of data, now returns empty results
            if not posts:
                logging.info("No more posts returned for search: %s", (q))
                break
            max_id = posts[-1]["id"]
            for post in posts:
                num_gabs += 1
                yield post
                if num_gabs > gabs and gabs != -1:
                    return

    def public_search(
        self,
        q,
        gabs=-1,
        gabs_after=(
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=20)
        ).strftime("%Y-%m-%dT%H:%M"),
    ):
        """
        Pass in a query.
        Searches the public Gab timeline for posts which match query q
        Match is case insensitive
        """

        num_gabs = 0
        max_id = ""
        while True:
            url = "https://gab.com/api/v1/timelines/public?limit=40&max_id=%s" % (
                max_id
            )

            resp = self.anonymous_get(url)
            # time.sleep(1)

            # We should probably implement some better error catching
            # not simply checking for a 500 to know we've gotten all the gabs possible
            if resp.status_code == 500:
                logging.error("search for %s failed, recieved 500 from Gab.com", (q))
                return
            elif resp.status_code == 429:
                logging.warn("rate limited, sleeping two minutes")
                time.sleep(100)
                break
            posts = resp.json()

            # API seems to be more stable than previously and will not send 500
            # as it runs out of data, now returns empty results
            if not posts:
                logging.info("No more posts returned for search: %s", (q))
                break

            for post in posts:
                if self.search_gab_text(post, q):
                    yield self.format_post(post)
                max_id = post["id"]
            num_gabs += len(posts)
            if num_gabs > gabs and gabs != -1:
                logging.info("Number of gabs condition met: %s", (q))
                break

            # Check if first collected gab is after the date specified
            # The API returns strange results sometimes where gabs are not in date order,
            # so ocassionally random dates pop up
            # But these never seem to appear at the start, so checking the first item keeps
            # the function from prematurely ending
            # It does mean an additional call is made however
            if posts[0]["created_at"] < gabs_after:
                if posts[1]["created_at"] < gabs_after:
                    if posts[2]["created_at"] < gabs_after:
                        if posts[-1]["created_at"] < gabs_after:
                            logging.info("Gabs after condition met: %s", (q))

                            break

    def user(self, q):
        """
        collect user json data
        """
        url = "https://gab.com/api/v1/account_by_username/%s" % (q)
        resp = self.get(url)
        yield resp.json()

    def top(self, timespan=None):
        if timespan is None:
            timespan = "today"
        assert timespan in ["today", "weekly", "monthly", "yearly"]

        url = "https://gab.com/api/v1/timelines/explore?sort_by=top_%s" % timespan

        resp = self.anonymous_get(url)
        return resp.json()

    def userposts(self, q, gabs=-1, gabs_after="2000-01-01"):
        """
        collect posts from a user feed
        """
        # We need to get the account id to collect statuses
        account_url = "https://gab.com/api/v1/account_by_username/%s" % (q)
        account_id = self.get(account_url).json()["id"]
        max_id = ""
        base_url = (
            "https://gab.com/api/v1/accounts/%s/statuses?exclude_replies=true&max_id="
            % (account_id)
        )

        num_gabs = 0
        while True:
            url = base_url + max_id
            resp = self.get(url)
            posts = resp.json()
            if not posts:
                break
            last_published_date = posts[-1]["created_at"]
            for post in posts:
                yield self.format_post(post)
                max_id = post["id"]
            num_gabs += len(posts)
            if last_published_date < gabs_after:
                break
            if num_gabs > gabs and gabs != -1:
                break

    def usercomments(self, q, gabs=-1):
        """
        collect comments from a users feed
        """
        # We need to get the account id to collect statuses
        account_url = 'https://gab.com/api/v1/account_by_username/%s' % (q)
        account_id = self.get(account_url).json()['id']
        max_id = ''
        base_url = "https://gab.com/api/v1/accounts/%s/statuses?only_comments=true&exclude_replies=false" % (account_id)
        actual_endpoint = base_url

        num_gabs = 0
        while True:
            url = actual_endpoint
            resp = self.get(url)
            posts = resp.json()
            if not posts:
                break
            for post in posts:
                yield self.format_post(post)
                max_id = post["id"]
            num_gabs += len(posts)
            actual_endpoint = base_url + 'max_id=' + max_id
            if  (num_gabs > gabs and gabs != -1):
                break

    def login(self):
        """
        Login to Gab to retrieve needed session token.
        """
        if not (self.user_account and self.user_password):
            raise RuntimeError("MissingAccountInfo")

        if self.cookie:
            logging.info("refreshing login cookie")

        url = "https://gab.com/auth/sign_in"
        input_token = requests.get(url, headers=self.headers)
        page_info = BeautifulSoup(input_token.content, "html.parser")
        token = page_info.select("meta[name=csrf-token]")[0]["content"]

        payload = {
            "user[email]": self.user_account,
            "user[password]": self.user_password,
            "authenticity_token": token,
        }

        d = requests.request(
            "POST",
            url,
            params=payload,
            cookies=input_token.cookies,
            headers=self.headers,
        )
        self.cookie = d.cookies

    def followers(self, q):
        """
        find all followers of a specific user
        This is currently broken
        """
        num_followers = 0
        while True:
            url = "https://gab.com/users/%s/followers?before=%s" % (q, num_followers)
            resp = self.get(url)
            posts = resp.json()["data"]
            if not posts:
                break
            for post in posts:
                yield post
            num_followers += len(posts)

    def following(self, q):
        """
        This is currently broken
        """
        num_followers = 0
        while True:
            url = "https://gab.com/users/%s/following?before=%s" % (q, num_followers)
            resp = self.get(url)
            posts = resp.json()["data"]
            if not posts:
                break
            for post in posts:
                yield post
            num_followers += len(posts)

    def get(self, url, **kwargs):
        """
        Perform the API requests
        """
        if not self.cookie:
            self.login()

        try:
            logging.info("getting %s %s", url, kwargs)

            r = requests.get(url, cookies=self.cookie, headers=self.headers)
            # Maybe should implement allow_404 that stops retrying, ala twarc

            if r.status_code == 404:
                logging.warn("404 from Gab API! trying again")
                time.sleep(10)
                r = self.get(url, **kwargs)
            if r.status_code == 500:
                logging.warn("500 from Gab API! trying again")
                time.sleep(15)
                r = self.get(url, **kwargs)
            return r
        except requests.exceptions.ConnectionError as e:
            logging.warn("Connection Error from Gab API! trying again")
            logging.debug(e)
            time.sleep(15)

            self.get(url, **kwargs)

    def anonymous_get(self, url, **kwargs):
        """
        Perform an anonymous API request. Used for accessing public timelines.
        """

        try:
            logging.info("getting %s %s", url, kwargs)
            r = requests.get(url, headers=self.headers)
            # Maybe should implement allow_404 that stops retrying, ala twarc

            if r.status_code == 404:
                logging.warn("404 from Gab API! trying again")
                time.sleep(15)
                r = self.anonymous_get(url, **kwargs)
            if r.status_code == 500:
                logging.warn("500 from Gab API! trying again")
                time.sleep(15)
                r = self.anonymous_get(url, **kwargs)
            return r
        except requests.exceptions.ConnectionError as e:
            logging.warn("Connection Error from Gab API! trying again")
            logging.debug(e)
            time.sleep(15)

            self.anonymous_get(url, **kwargs)

    def search_gab_text(self, gab, query):
        """
        Search if query exists within the text of a gab
        Return True if it does, False if not
        """
        if re.search(query.lower(), gab["content"].lower()):
            match = True
        else:
            match = False

        return match

    def format_post(self, post):
        """
        Format post so that body field is inserted, this harmonizes new mastodon data with old gab data
        """
        body = BeautifulSoup(
            html.unescape(post["content"]), features="html.parser"
        ).get_text()
        post["body"] = body
        return post

    def load_headers(self):
        config = configparser.ConfigParser()
        config.read(self.config)
        if "headers" not in config.sections():
            user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36"
        else:
            user_agent = config.get("headers", "user_agent")

        headers = {"User-Agent": user_agent}

        setattr(self, "headers", headers)

    def check_keys(self):
        """
        Get the Gab account info. Order of precedence is command line,
        environment, config file. Return True if all the keys were found
        and False if not.
        """
        env = os.environ.get
        if not self.user_account:
            self.user_account = env("GAB_ACCOUNT")
        if not self.user_password:
            self.user_password = env("GAB_PASSWORD")

        if self.config and not (self.user_account and self.user_password):
            self.load_config()

        return self.user_password and self.user_password

    def load_config(self):
        """
        Attempt to load gab info from config file
        """
        path = self.config
        profile = self.profile
        logging.info("loading %s profile from config %s", profile, path)

        if not path or not os.path.isfile(path):
            return {}

        config = configparser.ConfigParser()
        config.read(self.config)

        if profile not in config.sections():
            return {}

        data = {}
        for key in ["user_account", "user_password"]:
            try:
                setattr(self, key, config.get(profile, key))
            except configparser.NoSectionError:
                sys.exit("no such profile %s in %s" % (profile, path))
            except configparser.NoOptionError:
                sys.exit("missing %s from profile %s in %s" % (key, profile, path))
        return data

    def save_config(self):
        """
        Save new config file
        """
        if not self.config:
            return
        config = configparser.ConfigParser()
        config.read(self.config)
        config.add_section(self.profile)
        config.set(self.profile, "user_account", self.user_account)
        config.set(self.profile, "user_password", self.user_password)
        with open(self.config, "w") as config_file:
            config.write(config_file)

    def input_keys(self):
        """
        Create new config file with account info
        """
        print("\nPlease enter Gab account info.\n")

        def i(name):
            return get_input(name.replace("_", " ") + ": ")

        self.user_account = i("user_account")
        self.user_password = i("password")
        self.save_config()

    def default_config(self):
        """
        Default config file path
        """
        return os.path.join(os.path.expanduser("~"), ".garc")

    def save_user_agent(self):
        config = configparser.ConfigParser()
        config.read(self.config)
        print("\nPlease enter user_agent info.\n")

        def i(name):
            return get_input(name.replace("_", " ") + ": ")

        self.user_agent = i("user_agent")
        if "headers" not in config.sections():
            config.add_section("headers")
        config.set("headers", "user_agent", self.user_agent)
        with open(self.config, "w") as config_file:
            config.write(config_file)
