from collections import OrderedDict, defaultdict, namedtuple
from math import ceil, modf
from typing import Tuple, cast

from django.conf import settings
from django.core.cache import caches
from django.db.models import Exists, OuterRef, Q, Sum

from evap.evaluation.models import (
    CHOICES,
    NO_ANSWER,
    Contribution,
    Course,
    Evaluation,
    Question,
    Questionnaire,
    RatingAnswerCounter,
    TextAnswer,
    UserProfile,
)

STATES_WITH_RESULTS_CACHING = {Evaluation.State.EVALUATED, Evaluation.State.REVIEWED, Evaluation.State.PUBLISHED}
STATES_WITH_RESULT_TEMPLATE_CACHING = {Evaluation.State.PUBLISHED}


GRADE_COLORS = {
    1: (136, 191, 74),
    2: (187, 209, 84),
    3: (239, 226, 88),
    4: (242, 158, 88),
    5: (235, 89, 90),
}


class EvaluationResult:
    def __init__(self, contribution_results):
        self.contribution_results = contribution_results

    @property
    def questionnaire_results(self):
        return [
            questionnaire_result
            for contribution_result in self.contribution_results
            for questionnaire_result in contribution_result.questionnaire_results
        ]


class ContributionResult:
    def __init__(self, contributor, label, questionnaire_results):
        self.contributor = contributor
        self.label = label
        self.questionnaire_results = questionnaire_results

    @property
    def has_answers(self):
        for questionnaire_result in self.questionnaire_results:
            for question_result in questionnaire_result.question_results:
                question = question_result.question
                if question.is_text_question or question.is_rating_question and question_result.has_answers:
                    return True
        return False


class QuestionnaireResult:
    def __init__(self, questionnaire, question_results):
        self.questionnaire = questionnaire
        self.question_results = question_results


class RatingResult:
    def __init__(self, question, answer_counters, additional_text_result=None):
        assert question.is_rating_question
        self.question = question
        self.additional_text_result = additional_text_result

        if answer_counters is not None:
            counts = OrderedDict((value, 0) for value in self.choices.values if value != NO_ANSWER)
            for answer_counter in answer_counters:
                counts[answer_counter.answer] = answer_counter.count
            self.counts = tuple(counts.values())
        else:
            self.counts = None

    @property
    def choices(self):
        return CHOICES[self.question.type]

    @property
    def count_sum(self):
        if not self.is_published:
            return None
        return sum(self.counts)

    @property
    def minus_balance_count(self):
        assert self.question.is_bipolar_likert_question
        portion_left = sum(self.counts[:3]) + self.counts[3] / 2
        return (self.count_sum - portion_left) / 2

    @property
    def approval_count(self):
        assert self.question.is_yes_no_question
        if not self.is_published:
            return None
        return self.counts[0] if self.question.is_positive_yes_no_question else self.counts[1]

    @property
    def average(self):
        if not self.has_answers:
            return None
        return sum(grade * count for count, grade in zip(self.counts, self.choices.grades)) / self.count_sum

    @property
    def has_answers(self):
        return self.is_published and any(count != 0 for count in self.counts)

    @property
    def is_published(self):
        return self.counts is not None


class TextResult:
    def __init__(self, question, answers, answers_visible_to=None):
        assert question.can_have_textanswers
        self.question = question
        self.answers = answers
        self.answers_visible_to = answers_visible_to


HeadingResult = namedtuple("HeadingResult", ("question"))
TextAnswerVisibility = namedtuple("TextAnswerVisibility", ("visible_by_contribution", "visible_by_delegation_count"))


def get_single_result_rating_result(evaluation):
    assert evaluation.is_single_result

    answer_counters = RatingAnswerCounter.objects.filter(contribution__evaluation__pk=evaluation.pk)
    assert 1 <= len(answer_counters) <= 5

    question = Question.objects.get(questionnaire__name_en=Questionnaire.SINGLE_RESULT_QUESTIONNAIRE_NAME)
    return RatingResult(question, answer_counters)


def get_results_cache_key(evaluation):
    return f"evap.staff.results.tools.get_results-{evaluation.id:d}"


def cache_results(evaluation):
    assert evaluation.state in STATES_WITH_RESULTS_CACHING
    cache_key = get_results_cache_key(evaluation)
    caches["results"].set(cache_key, _get_results_impl(evaluation))


def get_results(evaluation):
    assert evaluation.state in STATES_WITH_RESULTS_CACHING | {Evaluation.State.IN_EVALUATION}

    if evaluation.state == Evaluation.State.IN_EVALUATION:
        return _get_results_impl(evaluation)

    cache_key = get_results_cache_key(evaluation)
    result = caches["results"].get(cache_key)
    assert result is not None
    return result


def _get_results_impl(evaluation):
    contributor_contribution_results = []
    for contribution in evaluation.contributions.all().prefetch_related("questionnaires", "questionnaires__questions"):
        questionnaire_results = []
        for questionnaire in contribution.questionnaires.all():
            results = []
            for question in questionnaire.questions.all():
                if question.is_heading_question:
                    results.append(HeadingResult(question=question))
                    continue
                text_result = None
                if question.can_have_textanswers and evaluation.can_publish_text_results:
                    answers = TextAnswer.objects.filter(
                        contribution=contribution,
                        question=question,
                        state__in=[TextAnswer.State.PRIVATE, TextAnswer.State.PUBLISHED],
                    )
                    text_result = TextResult(
                        question=question, answers=answers, answers_visible_to=textanswers_visible_to(contribution)
                    )
                if question.is_rating_question:
                    if evaluation.can_publish_rating_results:
                        answer_counters = RatingAnswerCounter.objects.filter(
                            contribution=contribution, question=question
                        )
                    else:
                        answer_counters = None
                    results.append(RatingResult(question, answer_counters, additional_text_result=text_result))
                elif question.is_text_question and evaluation.can_publish_text_results:
                    results.append(text_result)
            questionnaire_results.append(QuestionnaireResult(questionnaire, results))
        contributor_contribution_results.append(
            ContributionResult(contribution.contributor, contribution.label, questionnaire_results)
        )
    return EvaluationResult(contributor_contribution_results)


def annotate_distributions_and_grades(evaluations):
    for evaluation in evaluations:
        if not evaluation.is_single_result:
            evaluation.distribution = calculate_average_distribution(evaluation)
        else:
            evaluation.single_result_rating_result = get_single_result_rating_result(evaluation)
            evaluation.distribution = normalized_distribution(evaluation.single_result_rating_result.counts)
        evaluation.avg_grade = distribution_to_grade(evaluation.distribution)


def normalized_distribution(distribution):
    """Returns a normalized distribution with the individual values adding up to 1.
    Can also be used to convert counts to a distribution."""
    if distribution is None:
        return None

    distribution_sum = sum(distribution)
    if distribution_sum == 0:
        return None

    return tuple((value / distribution_sum) for value in distribution)


def unipolarized_distribution(result):
    summed_distribution = [0, 0, 0, 0, 0]

    if not result.counts:
        return None

    for counts, grade in zip(result.counts, result.choices.grades):
        grade_fraction, grade = modf(grade)
        grade = int(grade)
        summed_distribution[grade - 1] += (1 - grade_fraction) * counts
        if grade < 5:
            summed_distribution[grade] += grade_fraction * counts

    return normalized_distribution(summed_distribution)


def avg_distribution(weighted_distributions):
    if all(distribution is None for distribution, __ in weighted_distributions):
        return None

    summed_distribution = [0, 0, 0, 0, 0]
    for distribution, weight in weighted_distributions:
        if distribution:
            for index, value in enumerate(distribution):
                summed_distribution[index] += weight * value
    return normalized_distribution(summed_distribution)


def average_grade_questions_distribution(results):
    return avg_distribution(
        [
            (unipolarized_distribution(result), result.count_sum)
            for result in results
            if result.question.is_grade_question
        ]
    )


def average_non_grade_rating_questions_distribution(results):
    return avg_distribution(
        [
            (unipolarized_distribution(result), result.count_sum)
            for result in results
            if result.question.is_non_grade_rating_question
        ]
    )


def calculate_average_course_distribution(course, check_for_unpublished_evaluations=True):
    if check_for_unpublished_evaluations and course.evaluations.exclude(state=Evaluation.State.PUBLISHED).exists():
        return None

    return avg_distribution(
        [
            (
                (
                    calculate_average_distribution(evaluation)
                    if not evaluation.is_single_result
                    else normalized_distribution(get_single_result_rating_result(evaluation).counts)
                ),
                evaluation.weight,
            )
            for evaluation in course.evaluations.all()
        ]
    )


def get_evaluations_with_course_result_attributes(evaluations):
    courses_with_unpublished_evaluations = (
        Course.objects.filter(evaluations__in=evaluations)
        .filter(Exists(Evaluation.objects.filter(course=OuterRef("pk")).exclude(state=Evaluation.State.PUBLISHED)))
        .values_list("id", flat=True)
    )

    course_id_evaluation_weight_sum_pairs = (
        Course.objects.annotate(Sum("evaluations__weight"))
        .filter(pk__in=Course.objects.filter(evaluations__in=evaluations))  # is needed, see #1691
        .values_list("id", "evaluations__weight__sum")
    )

    evaluation_weight_sum_per_course_id = {entry[0]: entry[1] for entry in course_id_evaluation_weight_sum_pairs}

    for evaluation in evaluations:
        if evaluation.course.id in courses_with_unpublished_evaluations:
            evaluation.course.not_all_evaluations_are_published = True
            evaluation.course.distribution = None
        else:
            evaluation.course.distribution = calculate_average_course_distribution(evaluation.course, False)

        evaluation.course.evaluation_count = evaluation.course.evaluations.count()
        evaluation.course.avg_grade = distribution_to_grade(evaluation.course.distribution)
        evaluation.course.evaluation_weight_sum = evaluation_weight_sum_per_course_id[evaluation.course.id]

    return evaluations


def calculate_average_distribution(evaluation):
    assert evaluation.state >= Evaluation.State.IN_EVALUATION

    if not evaluation.can_staff_see_average_grade or not evaluation.can_publish_average_grade:
        return None

    # will contain a list of question results for each contributor and one for the evaluation (where contributor is None)
    grouped_results = defaultdict(list)
    for contribution_result in get_results(evaluation).contribution_results:
        for questionnaire_result in contribution_result.questionnaire_results:
            grouped_results[contribution_result.contributor].extend(questionnaire_result.question_results)

    evaluation_results = grouped_results.pop(None, [])

    average_contributor_distribution = avg_distribution(
        [
            (
                avg_distribution(
                    [
                        (
                            average_grade_questions_distribution(contributor_results),
                            settings.CONTRIBUTOR_GRADE_QUESTIONS_WEIGHT,
                        ),
                        (
                            average_non_grade_rating_questions_distribution(contributor_results),
                            settings.CONTRIBUTOR_NON_GRADE_RATING_QUESTIONS_WEIGHT,
                        ),
                    ]
                ),
                max(
                    (result.count_sum for result in contributor_results if result.question.is_rating_question),
                    default=0,
                ),
            )
            for contributor_results in grouped_results.values()
        ]
    )

    return avg_distribution(
        [
            (average_grade_questions_distribution(evaluation_results), settings.GENERAL_GRADE_QUESTIONS_WEIGHT),
            (
                average_non_grade_rating_questions_distribution(evaluation_results),
                settings.GENERAL_NON_GRADE_QUESTIONS_WEIGHT,
            ),
            (average_contributor_distribution, settings.CONTRIBUTIONS_WEIGHT),
        ]
    )


def distribution_to_grade(distribution):
    if distribution is None:
        return None
    return sum(answer * percentage for answer, percentage in enumerate(distribution, start=1))


def color_mix(color1, color2, fraction):
    return cast(
        Tuple[int, int, int], tuple(int(round(color1[i] * (1 - fraction) + color2[i] * fraction)) for i in range(3))
    )


def get_grade_color(grade):
    # Can happen if no one leaves any grades. Return white because it least likely causes problems.
    if not grade:
        return (255, 255, 255)
    grade = round(grade, 1)
    next_lower = int(grade)
    next_higher = int(ceil(grade))
    return color_mix(GRADE_COLORS[next_lower], GRADE_COLORS[next_higher], grade - next_lower)


def textanswers_visible_to(contribution):
    if contribution.is_general:
        contributors = (
            UserProfile.objects.filter(
                Q(
                    contributions__evaluation=contribution.evaluation,
                    contributions__textanswer_visibility=Contribution.TextAnswerVisibility.GENERAL_TEXTANSWERS,
                )
                | Q(courses_responsible_for__in=[contribution.evaluation.course])
            )
            .distinct()
            .order_by("last_name", "first_name")
        )
    else:
        contributors = [contribution.contributor]
    non_proxy_contributors = [contributor for contributor in contributors if not contributor.is_proxy_user]
    num_delegates = len(
        set(UserProfile.objects.filter(represented_users__in=non_proxy_contributors).distinct()) - set(contributors)
    )
    return TextAnswerVisibility(visible_by_contribution=contributors, visible_by_delegation_count=num_delegates)


def can_textanswer_be_seen_by(user, represented_users, textanswer, view):
    # pylint: disable=too-many-return-statements
    assert textanswer.state in [TextAnswer.State.PRIVATE, TextAnswer.State.PUBLISHED]
    contributor = textanswer.contribution.contributor

    if view == "public":
        return False

    if view == "export":
        if textanswer.is_private:
            return False
        if not textanswer.contribution.is_general and contributor != user:
            return False
    elif user.is_reviewer:
        return True

    if textanswer.is_private:
        return contributor == user

    # NOTE: when changing this behavior, make sure all changes are also reflected in results.tools.textanswers_visible_to
    # and in results.tests.test_tools.TestTextAnswerVisibilityInfo
    if textanswer.is_published:
        # users can see textanswers if the contributor is one of their represented users (which includes the user itself)
        if contributor in represented_users:
            return True
        # users can see text answers from general contributions if one of their represented users has text answer
        # visibility GENERAL_TEXTANSWERS for the evaluation
        if (
            textanswer.contribution.is_general
            and textanswer.contribution.evaluation.contributions.filter(
                contributor__in=represented_users,
                textanswer_visibility=Contribution.TextAnswerVisibility.GENERAL_TEXTANSWERS,
            ).exists()
        ):
            return True
        # the people responsible for a course can see all general text answers for all its evaluations
        if textanswer.contribution.is_general and any(
            user in represented_users for user in textanswer.contribution.evaluation.course.responsibles.all()
        ):
            return True

    return False
