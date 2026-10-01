"""Text shaping for downward inflection: declarative sentences that end in periods."""

from musichistory.narration import textshape


def test_questions_and_exclamations_become_statements():
    assert textshape.shape("Is this the same progression?") == "Is this the same progression."
    assert textshape.shape("It is the same four chords!") == "It is the same four chords."
    assert textshape.shape("Really?! Yes.") == "Really. Yes."


def test_mid_sentence_marks_become_pauses():
    assert textshape.shape("It moves... to the four chord") == "It moves, to the four chord."
    assert textshape.shape("And then?  the bridge") == "And then, the bridge."


def test_final_period_and_whitespace():
    assert textshape.shape("  The loop   repeats ,  again  ") == "The loop repeats, again."
    assert textshape.shape("It ends on the tonic,") == "It ends on the tonic."
    assert textshape.shape("It ends here.") == "It ends here."
    assert textshape.shape('He called them "the changes"') == 'He called them "the changes".'
    assert textshape.shape("") == ""


def test_is_shaped():
    assert textshape.is_shaped("One. Two.")
    assert textshape.is_shaped('He said "yes."')
    assert not textshape.is_shaped("Why?")
    assert not textshape.is_shaped("No period")
    assert not textshape.is_shaped("")


def test_sentences_respect_abbreviations_initials_and_decimals():
    s = textshape.sentences("Mr. Smith wrote it in 1965. J. S. Bach used it at 1.5 times the speed. It stuck.")
    assert s == ["Mr. Smith wrote it in 1965.", "J. S. Bach used it at 1.5 times the speed.", "It stuck."]
    assert textshape.sentences("One line only.") == ["One line only."]


def test_words():
    assert textshape.words("The I-IV-V isn't new, in 1955.") == 8   # I, IV, V are spoken as three words
