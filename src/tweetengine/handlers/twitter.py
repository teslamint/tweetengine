import urlparse

from google.appengine.api import users

from tweetengine.handlers import base
from tweetengine import model
from tweetengine import oauth


class TweetHandler(base.BaseHandler):
    @base.requires_account
    def post(self, current_account):
        tweet = model.OutgoingTweet(account=self.current_account,
                                    user=self.user_account,
                                    approved_by=self.user_account,
                                    message=self.request.get("tweet"))
        tweet.put()
        
        self.render_template("me.html", {"account": self.current_account})