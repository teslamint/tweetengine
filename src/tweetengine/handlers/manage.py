import hashlib
import hmac
import itertools
import logging
import os
import urllib
import uuid
import urlparse
from django import newforms as forms
from django.newforms import widgets
from google.appengine.api import mail
from google.appengine.ext import db
from google.appengine.ext.webapp import template
from tweetengine.handlers import base
from tweetengine import model


class TabularRadioInput(widgets.RadioInput):
    def __unicode__(self):
        return self.tag()


class TabularRadioFieldRenderer(widgets.RadioFieldRenderer):
    def __iter__(self):
        for i, choice in enumerate(self.choices):
            yield TabularRadioInput(self.name, self.value,
                                    self.attrs.copy(), choice, i)

    def __unicode__(self):
        return u'</td><td>'.join([unicode(x) for x in self])


class TabularRadioSelect(widgets.RadioSelect):
    """A radio select widget that outputs table cells."""
    def render(self, name, value, attrs=None, choices=()):
        if value is None: value = ''
        str_value = widgets.smart_unicode(value)
        attrs = attrs or {}
        return TabularRadioFieldRenderer(
            name, str_value, attrs, list(itertools.chain(self.choices, choices)))


class SettingsForm(forms.Form):
    suggest_tweets = forms.ChoiceField(choices=model.ROLES, widget=TabularRadioSelect)
    review_tweets = forms.ChoiceField(choices=model.ROLES, widget=TabularRadioSelect)
    send_tweets = forms.ChoiceField(choices=model.ROLES, widget=TabularRadioSelect)


class ManageHandler(base.UserHandler):
    @base.requires_account_admin
    def get(self, account_name, settings_form=None):
        if not settings_form:
            settings_form = SettingsForm(initial={
                "suggest_tweets": self.current_account.suggest_tweets,
                "send_tweets": self.current_account.send_tweets,
                "review_tweets": self.current_account.review_tweets,
            })
        permissions = self.current_account.permission_set.fetch(100)
        base.prefetch_refprops(permissions, model.Permission.user)
        my_key = self.current_permission.key()
        self.render_template("manage.pt", {
            "acct_permissions": permissions,
            "my_key": my_key,
            "sent": self.request.GET.get("sent", False),
            "settings_form": settings_form,
            "allow_public": model.Configuration.instance().allow_public
        })

    @base.requires_account_admin
    def post(self, account_name):
        settings_form = SettingsForm(self.request.POST)
        if settings_form.is_valid():
            for k, v in settings_form.clean_data.items():
                setattr(self.current_account, k, int(v))
            self.current_account.put()
            self.redirect("/%s/manage?saved=true" % (self.current_account.username,))
        else:
            self.get(account_name, settings_form)
        

class ManageUsersHandler(base.UserHandler):
    @base.requires_account_admin
    def post(self, account_name):
        permissions = self.current_account.permission_set.fetch(100)
        permission_map = dict((x.key().id(), x) for x in permissions)
        my_id = self.current_permission.key().id()
        
        # Handle deletion
        to_delete = [permission_map[int(x)]
                     for x in self.request.POST.getall("delete")
                     if int(x) in permission_map and x != my_id]
        db.delete(to_delete)
        
        # Handle permission changes
        new_permissions = [(int(k.split(".")[1]), int(v))
                           for k, v in self.request.POST.iteritems()
                           if k.startswith("permission.")]
        to_update = []
        for perm_id, role in new_permissions:
            if perm_id == my_id:
                continue
            permission = permission_map[perm_id]
            if permission.role != role:
                permission.role = role
                to_update.append(permission)
        db.put(to_update)
        
        # Handle new users
        invites = zip(self.request.POST.getall("username"),
                      self.request.POST.getall("new_permission"))
        invites = [x for x in invites if x[0]]
        self.send_invites(invites)
                
        if invites:
            self.redirect("/%s/manage?sent=true" % (self.current_account.username,))
        else:
            self.redirect("/%s/manage" % (self.current_account.username,))

    def send_invites(self, invites):
        config = model.Configuration.instance()
        template_path = os.path.join(os.path.dirname(__file__), "..",
                                     "templates", "email.txt")
        account_username = self.current_account.username
        for username, role in invites:
            nonce = str(uuid.uuid4())
            mac_data = ":".join([account_username, role, nonce])
            mac = hmac.new(config.oauth_secret, mac_data, hashlib.sha1).hexdigest()
            qs = urllib.urlencode({
                "role": role,
                "nonce": nonce,
                "mac": mac
            })
            url = urlparse.urljoin(
                self.request.url,
                "/%s/invite?%s" % (account_username, qs))
            email_body = template.render(template_path, {
                "account_username": account_username,
                "username": username,
                "url": url
            })
            logging.info(email_body)
            subject = "You have been invited to tweet as %s!" % (account_username,)
            mail.send_mail(config.mail_from, username, subject, email_body)


class InviteHandler(base.UserHandler):
    @base.requires_account
    def get(self, account_name):
        config = model.Configuration.instance()
        role = self.request.GET.get("role", model.ROLE_USER)
        nonce = self.request.GET["nonce"]
        
        # Verify the mac
        mac_data = ":".join([account_name, role, nonce])
        mac = hmac.new(config.oauth_secret, mac_data, hashlib.sha1).hexdigest()
        if mac != self.request.GET["mac"]:
            logging.error("Invalid MAC")
            self.error(400)
            return
        
        # Check the nonce hasn't been reused
        if model.Permission.all().filter("invite_nonce =", nonce).count():
            logging.error("Reused nonce")
            self.error(400)
            return
        
        # Add the permission record
        permission = model.Permission.find(self.user_account, self.current_account)
        if not permission:
            permission = model.Permission.create(
                self.user_account,
                self.current_account,
                int(role),
                nonce)
            permission.put()
        self.redirect("/%s/" % (self.current_account.username,))
