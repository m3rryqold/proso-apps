from django.shortcuts import get_object_or_404
from models import Question, Option, DecoratedAnswer, Set, Category
from proso_models.models import Answer
from proso.django.response import render, render_json, redirect_pass_get
from django.views.decorators.csrf import ensure_csrf_cookie
from lazysignup.decorators import allow_lazy_user
from django.http import HttpResponse, HttpResponseBadRequest
from ipware.ip import get_ip
from django.db import transaction
import json_enrich
import proso_common.json_enrich as common_json_enrich
from proso.django.request import get_time, get_user_id
from proso_models.models import get_environment, get_recommendation
from proso_ab.models import Experiment, Value
import logging
from time import time as time_lib
from proso.django.cache import cache_page_conditional
import hashlib
from django.core.cache import cache
import json as json_lib


LOGGER = logging.getLogger('django.request')


def home(request):
    return render(request, 'questions_home.html', {})


@cache_page_conditional(condition=lambda request: 'stats' not in request.GET)
def show_one(request, object_class, id):
    """
    Return object of the given type with the specified identifier.

    GET parameters:
      user:
        identifier of the current user
      stats:
        turn on the enrichment of the objects by some statistics
      html
        turn on the HTML version of the API
    """
    obj = get_object_or_404(object_class, pk=id)
    json = _to_json(request, obj)
    return render_json(request, json, template='questions_json.html', help_text=show_one.__doc__)


@cache_page_conditional(condition=lambda request: 'stats' not in request.GET)
def show_more(request, object_class, should_cache=True):
    """
    Return list of objects of the given type.

    GET parameters:
      limit:
        number of returned questions (default 10, maximum 100)
      page:
        current page number
      filter_column:
        column name used to filter the results
      filter_value:
        value for the specified column used to filter the results
      user:
        identifier of the current user
      all:
        return all objects available instead of paging; be aware this parameter
        can be used only for objects for wich the caching is turned on
      db_orderby:
        database column which the result should be ordered by
      json_orderby:
        field of the JSON object which the result should be ordered by, it is
        less effective than the ordering via db_orderby; be aware this parameter
        can be used only for objects for which the caching is turned on
      desc
        turn on the descending order
      stats:
        turn on the enrichment of the objects by some statistics
      html
        turn on the HTML version of the API
    """
    if not should_cache and 'json_orderby' in request.GET:
        return render_json(request, {
            'error': "Can't order the result according to the JSON field, because the caching for this type of object is turned off. See the documentation."
            },
            template='questions_json.html', help_text=show_more.__doc__, status=501)
    if not should_cache and 'all' in request.GET:
        return render_json(request, {
            'error': "Can't get all objects, because the caching for this type of object is turned off. See the documentation."
            },
            template='questions_json.html', help_text=show_more.__doc__, status=501)
    time_start = time_lib()
    limit = min(int(request.GET.get('limit', 10)), 100)
    page = int(request.GET.get('page', 0))
    select_related_all = {
        Question: ['resource'],
        DecoratedAnswer: ['general_answer']
    }
    prefetch_related_all = {
        Set: ['questions'],
        Category: ['questions'],
        Question: [
            'question_options', 'question_options__option_images',
            'question_images', 'resource__resource_images', 'set_set', 'category_set'
        ]
    }
    select_related = select_related_all.get(object_class, [])
    prefetch_related = prefetch_related_all.get(object_class, [])
    if 'filter_column' in request.GET and 'filter_value' in request.GET:
        column = request.GET['filter_column']
        value = request.GET['filter_value']
        if value.isdigit():
            value = int(value)
        if column == 'category_id':
            objs = (get_object_or_404(Category, pk=value).
                    questions.
                    select_related(*select_related).
                    prefetch_related(*prefetch_related).all())
        elif column == 'set_id':
            objs = (get_object_or_404(Set, pk=value).
                    questions.
                    select_related(*select_related).
                    prefetch_related(*prefetch_related).all())
        else:
            objs = (object_class.objects.
                    select_related(*select_related).
                    prefetch_related(*prefetch_related).filter(**{column: value}))
    else:
        objs = object_class.objects.select_related(*select_related).prefetch_related(*prefetch_related).all()
    if object_class == DecoratedAnswer:
        if 'user' in request.GET and request.user.is_staff():
            user_id = int(request.GET['user'])
        else:
            user_id = request.user.id
        objs = objs.filter(general_answer__user_id=user_id).order_by('-general_answer__time')
    if 'db_orderby' in request.GET:
        objs = objs.order_by(('-' if 'desc' in request.GET else '') + request.GET['db_orderby'])
    if 'all' not in request.GET and 'json_orderby' not in request.GET:
        objs = objs[page * limit:(page + 1) * limit]
    cache_key = 'proso_questions_sql_json_%s' % hashlib.sha1(str(objs.query).decode('utf-8')).hexdigest()
    cached = cache.get(cache_key)
    if cached:
        list_objs = json_lib.loads(cached)
    else:
        list_objs = map(lambda x: x.to_json(), list(objs))
        cache.set(cache_key, json_lib.dumps(list_objs), 60 * 60 * 24 * 30)
    LOGGER.debug('loading objects in show_more view took %s seconds', (time_lib() - time_start))
    json = _to_json(request, list_objs)
    if 'json_orderby' in request.GET:
        time_before_json_sort = time_lib()
        json.sort(key=lambda x: (-1 if 'desc' in request.GET else 1) * x[request.GET['json_orderby']])
        if 'all' not in request.GET:
            json = json[page * limit:(page + 1) * limit]
        LOGGER.debug('sorting objects according to JSON field took %s seconds', (time_lib() - time_before_json_sort))
    return render_json(request, json, template='questions_json.html', help_text=show_more.__doc__)


@ensure_csrf_cookie
@allow_lazy_user
@transaction.atomic
def practice(request):
    """
    Return the given number of questions to practice adaptively. In case of
    POST request, try to save the answer.

    GET parameters:
      limit:
        number of returned questions (default 10, maximum 100)
      category:
        identifier for the category which is meant to be practiced
      time:
        time in format '%Y-%m-%d_%H:%M:%S' used for practicing
      user:
        identifier for the practicing user (only for stuff users)
      stats:
        turn on the enrichment of the objects by some statistics
      html
        turn on the HTML version of the API

    POST parameters:
      question:
        identifer for the asked question
      answered:
        identifier for the answered option
      response_time:
        number of milliseconds the given user spent by answering the question
    """
    limit = min(int(request.GET.get('limit', 10)), 100)
    # prepare
    user = get_user_id(request)
    time = get_time(request)
    environment = get_environment()
    recommendation = get_recommendation()
    if request.user.is_staff and 'time' in request.GET:
        environment.shift_time(time)
    category = request.GET.get('category', None)
    questions = None
    if category is not None:
        questions = get_object_or_404(Category, pk=category).questions.all()
    # save answers
    status = 200
    if request.method == 'POST':
        _save_answers(request)
        status = 201
    # recommend
    time_before_practice = time_lib()
    candidates = Question.objects.practice(recommendation, environment, user, time, limit, questions=questions)
    LOGGER.debug('choosing candidates for practice took %s seconds', (time_lib() - time_before_practice))
    json = _to_json(request, candidates)
    return render_json(request, json, template='questions_json.html', status=status, help_text=practice.__doc__)


@allow_lazy_user
def test(request):
    """
    Return questions to perform a test.

    GET parameters:
      time:
        time in format '%Y-%m-%d_%H:%M:%S' used for practicing
      user:
        identifier for the practicing user (only for stuff users)
      stats:
        turn on the enrichment of the objects by some statistics
      html
        turn on the HTML version of the API
    """
    user = get_user_id(request)
    time = get_time(request)
    candidates = Question.objects.test(user, time)
    json = _to_json(request, candidates)
    return render_json(request, json, template='questions_json.html', help_text=test.__doc__)


@ensure_csrf_cookie
@allow_lazy_user
@transaction.atomic
def answer(request):
    """
    Save the answer.

    GET parameters:
      html
        turn on the HTML version of the API

    POST parameters:
      question:
        identifer for the asked question
      answered:
        identifier for the answered option
      response_time:
        number of milliseconds the given user spent by answering the question
    """
    if request.method == 'GET':
        return render(request, 'questions_answer.html', {}, help_text=answer.__doc__)
    elif request.method == 'POST':
        saved_answers = _save_answers(request)
        if 'html' in request.GET and len(saved_answers) == 1:
            return redirect_pass_get(request, 'show_answer', id=saved_answers[0].id)
        else:
            return HttpResponse('ok', status=201)
    else:
        return HttpResponseBadRequest("method %s is not allowed".format(request.method))


def _to_json(request, value):
    time_start = time_lib()
    if isinstance(value, list):
        json = map(lambda x: x if isinstance(x, dict) else x.to_json(), value)
    elif not isinstance(value, dict):
        json = value.to_json()
    else:
        json = value
    LOGGER.debug("converting value to simple JSON took %s seconds", (time_lib() - time_start))
    common_json_enrich.enrich_by_object_type(request, json, json_enrich.question, 'answer')
    if 'stats' in request.GET:
        common_json_enrich.enrich_by_object_type(
            request, json, json_enrich.prediction, ['question', 'set', 'category'])
    common_json_enrich.enrich_by_object_type(request, json, json_enrich.html, ['question', 'resource', 'option'])
    common_json_enrich.enrich_by_predicate(request, json, json_enrich.url, lambda x: True)
    common_json_enrich.enrich_by_object_type(request, json, json_enrich.questions, ['set', 'category'])
    LOGGER.debug("converting value to JSON took %s seconds", (time_lib() - time_start))
    return json


def _save_answers(request):
    if len(request.POST.getlist('question', [])) == 0:
        return HttpResponseBadRequest('"question" is not defined')
    if len(request.POST.getlist('answered', [])) == 0:
        return HttpResponseBadRequest('"answered" is not defined')
    if len(request.POST.getlist('response_time', [])) == 0:
        return HttpResponseBadRequest('"response_time" is not defined')
    all_data = zip(
        request.POST.getlist('question'),
        request.POST.getlist('answered'),
        map(int, request.POST.getlist('response_time'))
    )
    ab_values = Value.objects.filter(id__in=map(lambda d: d['id'], Experiment.objects.get_values(request)))
    saved_answers = []
    for question_id, option_answered_id, response_time in all_data:
        question = Question.objects.get(pk=question_id)
        option_answered = Option.objects.get(pk=option_answered_id)
        option_asked = Option.objects.get_correct_option(question)

        answer = Answer(
            user_id=request.user.id,
            item_id=question.item_id,
            item_asked_id=option_asked.item_id,
            item_answered_id=option_answered.item_id,
            response_time=response_time)
        answer.save()
        decorated_answer = DecoratedAnswer(
            general_answer=answer,
            ip_address=get_ip(request))
        decorated_answer.save()
        for value in ab_values:
            decorated_answer.ab_values.add(value)
        decorated_answer.save()
        saved_answers.append(decorated_answer)
    return saved_answers
