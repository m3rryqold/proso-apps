from proso.django.response import render, render_json
import django.contrib.auth as auth
from proso.django.request import get_user_id, json_body
from models import Session, UserProfile
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import ensure_csrf_cookie
from lazysignup.decorators import allow_lazy_user
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.contrib.auth.models import User
from django.middleware.csrf import get_token
from django.utils import simplejson


def home(request):
    return render(request, 'user_home.html', {})


@allow_lazy_user
@transaction.atomic
def profile(request):
    """
    Get the user's profile. If the user has no assigned profile, the HTTP 404
    is returned. Make a POST request to modify the user's profile.

    GET parameters:
        html
            turn on the HTML version of the API

    POST parameters (JSON):
        send_emails:
            switcher turning on sending e-mails to user
        public:
            swicher making the user's profile publicly available
        user:
            password:
                user's password
            password_check:
                user's password again to check it
            first_name (optional):
                user's first name
            last_name (optional):
                user's last name
    """
    if request.method == 'GET':
        user_id = get_user_id(request)
        user_profile = get_object_or_404(UserProfile, user_id=user_id)
        return render_json(
            request, _to_json(request, user_profile),
            template='user_profile.html', help_text=profile.__doc__)
    elif request.method == 'POST':
        to_save = json_body(request.body)
        print to_save
        user_id = get_user_id(request)
        user_profile = get_object_or_404(UserProfile, user_id=user_id)
        user = to_save.get('user', None)
        if 'send_emails' in to_save:
            user_profile.send_emails = bool(to_save['send_emails'])
        if 'public' in to_save:
            user_profile.public = bool(to_save['public'])
        if user:
            error = _save_user(request, user, new=False)
            if error:
                return render_json(request, error, template='user_json.html', status=400)
        user_profile.save()
        return HttpResponse('ok', status=202)
    else:
        return HttpResponseBadRequest("method %s is not allowed".format(request.method))


@ensure_csrf_cookie
def login(request):
    """
    Log in

    GET parameters:
        html
            turn on the HTML version of the API

    POST parameters (JSON):
        username:
            user's name
        password:
            user's password
    """
    if request.method == 'GET':
        return render(request, 'user_login.html', {}, help_text=login.__doc__)
    elif request.method == 'POST':
        credentials = json_body(request.body)
        user = auth.authenticate(
            username=credentials.get('username', ''),
            password=credentials.get('password', ''),
        )
        if user is None:
            return render_json(request, {
                'error': 'Password or username does not match.',
                'error_type': 'password_username_not_match'
            }, template='user_json.html', status=401)
        if not user.is_active:
            return render_json(request, {
                'error': 'The account has not been activated.',
                'error_type': 'account_not_activated'
            }, template='user_json.html', status=401)
        auth.login(request, user)
        return profile(request)
    else:
        return HttpResponseBadRequest("method %s is not allowed".format(request.method))


def logout(request):
    auth.logout(request)
    return HttpResponse('ok', status=202)


@allow_lazy_user
@transaction.atomic
def signup(request):
    """
    Create a new user with the given credentials.

    GET parameters:
        html
            turn on the HTML version of the API

    POST parameters (JSON):
        username:
            user's name
        email:
            user's e-mail
        password:
            user's password
        password_check:
            user's password again to check it
        first_name (optional):
            user's first name
        last_name (optional):
            user's last name
    """
    if request.method == 'GET':
        return render(request, 'user_signup.html', {}, help_text=signup.__doc__)
    elif request.method == 'POST':
        credentials = json_body(request.body)
        error = _save_user(request, credentials, new=True)
        if error is not None:
            return render_json(request, error, template='user_json.html', status=400)
        else:
            user_profile = UserProfile(user=request.user)
            user_profile.save()
            return HttpResponse('ok', status=201)
    else:
        return HttpResponseBadRequest("method %s is not allowed".format(request.method))


@ensure_csrf_cookie
@allow_lazy_user
@transaction.atomic
def session(request):
    """
    Get the information about the current session or modify the current session.

    GET parameters:
      html
        turn on the HTML version of the API

    POST parameters:
      locale:
        client's locale
      time_zone:
        client's time zone
      display_width:
        width of the client's display
      display_height
        height of the client's display
    """
    if request.method == 'GET':
        return render_json(
            request,
            _to_json(request, Session.objects.get_current_session()),
            template='user_session.html', help_text=session.__doc__)
    elif request.method == 'POST':
        current_session = Session.objects.get_current_session()
        if current_session is None:
            return HttpResponseBadRequest("there is no current session to modify")
        locale = request.POST.get('locale', None)
        time_zone = request.POST.get('time_zone', None)
        display_width = request.POST.get('display_width', None)
        display_height = request.POST.get('display_height', None)
        if locale:
            current_session.locale = locale
        if time_zone:
            current_session.time_zone = time_zone
        if display_width:
            current_session.display_width = display_width
        if display_height:
            current_session.display_height = display_height
        current_session.save()
        return HttpResponse('ok', status=202)
    else:
        return HttpResponseBadRequest("method %s is not allowed".format(request.method))


def initmobile_view(request):
    """
    Create lazy user with a password. Used from the Android app.
    Also returns csrf token.

    GET parameters:
        username:
            user's name
        password:
            user's password
    """
    if 'username' in request.GET and 'password' in request.GET:
        username = request.GET['username']
        password = request.GET['password']
        user = auth.authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)
    else:
        user = request.user
    response = {
        'username': user.username,
        'csrftoken': get_token(request),
    }
    if not user.has_usable_password():
        password = User.objects.make_random_password()
        user.set_password(password)
        user.save()
        response['password'] = password
    return HttpResponse(simplejson.dumps(response))


def _to_json(request, value):
    if isinstance(value, list):
        json = map(lambda x: x if isinstance(x, dict) else x.to_json(), value)
    elif not isinstance(value, dict):
        json = value.to_json()
    else:
        json = value
    return json


def _check_credentials(credentials, new=False):
    if new and 'username' not in credentials:
        return {
            'error': 'There is no username',
            'error_type': 'username_empty'
        }
    if new and 'email' not in credentials:
        return {
            'error': 'There is no e-mail',
            'error_type': 'email_empty'
        }
    if new and 'password' not in credentials:
        return {
            'error': 'There is no password',
            'error_type': 'password_empty'
        }

    if 'password' in credentials and credentials['password'] != credentials.get('password_check'):
        return {
            'error': 'Passwords do not match.',
            'error_type': 'password_not_match'
        }
    if 'username' in credentials and _user_exists(username=credentials['username']):
        return {
            'error': 'There is already a user with the given username.',
            'error_type': 'username_exists'
        }
    if new and _user_exists(email=credentials['email']):
        return {
            'error': 'There is already a user with the given e-mail.',
            'error_type': 'email_exists'
        }
    return None


def _save_user(request, credentials, new=False):
    error = _check_credentials(credentials, new)
    if error is not None:
        return error
    else:
        user = request.user
        if new:
            user.username = credentials['username']
            user.email = credentials['email']
        if credentials.get('password'):
            user.set_password(credentials['password'])
        if credentials.get('first_name'):
            user.first_name = credentials['first_name']
        if credentials.get('last_name'):
            user.last_name = credentials['last_name']
        user.save()
        return None


def _user_exists(**kwargs):
    try:
        User.objects.get(**kwargs)
        return True
    except User.DoesNotExist:
        return False