import pytest

from studycenter_ice.decision import candidates, course_matches, plausible


COURSES = [
    {'id': 4, 'title': 'Computer Organization and Networks', 'semester': '2026W', 'aliases': ['con'], 'course_type': 'VU'},
    {'id': 14, 'title': 'Messtechnik (VO)', 'semester': '2026W', 'aliases': ['m', 'mt']},
    {'id': 15, 'title': 'Messtechnik, Übung (WS) [453012]', 'semester': '2026W', 'aliases': ['mu', 'mt']},
]


@pytest.mark.parametrize('text', ['morgen die Übung vorbereiten', 'Vorlesung', 'Übungen', 'WS 2026', '2026W', 'Semester', 'UE'])
def test_course_format_and_semester_do_not_identify_a_course(text):
    assert candidates(text, COURSES) == []
    assert course_matches(text, COURSES) == []


def test_split_spoken_course_alias_outvotes_no_generic_decorator():
    matches = candidates('Für K.O. in morgen die Übung vorbereiten.', COURSES)
    assert [(match.id, match.match) for match in matches] == [(4, 'phonetic')]
    assert [match.id for match in plausible(matches)] == [4]


@pytest.mark.parametrize('text', ['Messtechnik', 'Misstechnik', 'Miss Technik', 'MT'])
def test_named_course_ambiguity_between_lecture_and_exercise_is_retained(text):
    assert {course['id'] for course in course_matches(text, COURSES)} == {14, 15}


def test_explicit_user_alias_remains_authoritative_even_when_it_is_a_generic_word():
    courses = [dict(COURSES[1], learned_aliases=['Übung'])]
    matches = candidates('morgen die Übung vorbereiten', courses)
    assert [(match.id, match.score, match.match) for match in matches] == [(14, 1.1, 'alias')]


@pytest.mark.parametrize('text,expected', [('Messtechnik UE',15), ('Messtechnik Übung',15),
                                         ('Messtechnik VO',14), ('Messtechnik Vorlesung',14)])
def test_explicit_qualifier_disambiguates_only_an_identified_subject(text, expected):
    assert [match.id for match in candidates(text, COURSES)] == [expected]
    assert [course['id'] for course in course_matches(text, COURSES)] == [expected]


def test_qualifier_without_a_matching_course_type_does_not_discard_named_course():
    assert [match.id for match in candidates('CON morgen die Übung vorbereiten', COURSES)] == [4]


@pytest.mark.parametrize('text', ['Messtechnik VO und UE', 'Messtechnik Vorlesung und Übung', 'Messtechnik nicht Übung'])
def test_conflicting_or_negated_qualifiers_keep_subject_ambiguity(text):
    assert {match.id for match in candidates(text, COURSES)} == {14,15}
    assert {course['id'] for course in course_matches(text, COURSES)} == {14,15}


def test_explicit_exercise_qualifier_selects_parenthesized_ue_type():
    courses=[{'id':10,'title':'Entwurf von Echtzeitsystemen (VO)','semester':'2026W'},
             {'id':11,'title':'Entwurf von Echtzeitsystemen (UE)','semester':'2026W'}]
    assert [match.id for match in candidates('Entwurf von Echtzeitsystemen Übung',courses)] == [11]
    assert [course['id'] for course in course_matches('Entwurf von Echtzeitsystemen Übung',courses)] == [11]
