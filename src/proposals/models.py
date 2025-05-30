import contextlib

from django.conf import settings
from django.contrib.contenttypes.fields import (
    GenericForeignKey,
    GenericRelation,
)
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from core.models import (
    CATEGORY_CHOICES,
    BigForeignKey,
    ConferenceRelated,
    DefaultConferenceManager,
    EAWTextField,
    EventInfo,
)


class PrimarySpeaker:
    """A wapper representing the submitter of the proposal as a speaker.

    This class is meant to be compatible with ``AdditionalSpeaker``, and used
    along side with instances of that class.
    """

    def __init__(self, *, proposal=None, user=None):
        if proposal is None and user is None:
            raise ValueError('must specify either proposal or user')
        super().__init__()
        self._proposal = proposal
        self._user = user or proposal.submitter

    def __repr__(self):
        return f'<PrimarySpeaker: {self.user.speaker_name}>'

    def __eq__(self, other):
        return (
            isinstance(other, PrimarySpeaker) and
            self.user == other.user and
            self.proposal == other.proposal
        )

    @property
    def user(self):
        return self._user

    @property
    def proposal(self):
        return self._proposal

    @property
    def cancelled(self):
        return False

    def get_status_display(self):
        return gettext('Proposal author')


class AdditionalSpeaker(ConferenceRelated):

    user = BigForeignKey(
        to=settings.AUTH_USER_MODEL,
        verbose_name=_('user'),
        on_delete=models.CASCADE,
    )

    proposal_type = models.ForeignKey(
        to='contenttypes.ContentType',
        verbose_name=_('proposal model type'),
        on_delete=models.CASCADE,
    )
    proposal_id = models.BigIntegerField(
        verbose_name=_('proposal ID'),
    )
    proposal = GenericForeignKey('proposal_type', 'proposal_id')

    SPEAKING_STATUS_PENDING = 'pending'
    SPEAKING_STATUS_ACCEPTED = 'accepted'
    SPEAKING_STATUS_DECLINED = 'declined'
    SPEAKING_STATUS = (
        (SPEAKING_STATUS_PENDING, _('Pending')),
        (SPEAKING_STATUS_ACCEPTED, _('Accepted')),
        (SPEAKING_STATUS_DECLINED, _('Declined')),
    )
    status = models.CharField(
        max_length=8,
        choices=SPEAKING_STATUS,
        default=SPEAKING_STATUS_PENDING,
    )

    cancelled = models.BooleanField(
        verbose_name=_('cancelled'),
        default=False,
        db_index=True,
    )

    class Meta:
        unique_together = ['user', 'proposal_type', 'proposal_id']
        ordering = ['proposal_type', 'proposal_id', 'pk']
        verbose_name = _('additional speaker')
        verbose_name_plural = _('additional speakers')

    def __str__(self):
        return f'{self.user.speaker_name} ({self.get_status_display()})'


class ProposalQuerySet(models.QuerySet):

    def filter_accepted(self):
        return self.filter(cancelled=False, accepted=True)

    def filter_viewable(self, user):
        return self.filter(
            Q(submitter=user) |
            Q(additionalspeaker_set__in=user.cospeaking_info_set.all())
        )

    def filter_reviewable(self, user):
        return self.exclude(
            Q(cancelled=True) |
            Q(submitter=user) |
            Q(additionalspeaker_set__in=user.cospeaking_info_set.all())
        )


class AbstractProposal(ConferenceRelated, EventInfo):

    submitter = BigForeignKey(
        to=settings.AUTH_USER_MODEL,
        verbose_name=_('submitter'),
        on_delete=models.CASCADE,
    )

    outline = models.TextField(
        verbose_name=_('outline'),
        blank=True,
    )

    objective = EAWTextField(
        verbose_name=_('objective'),
        max_length=1000,
    )

    supplementary = models.TextField(
        verbose_name=_('supplementary'),
        blank=True,
        default='',
    )

    cancelled = models.BooleanField(
        verbose_name=_('cancelled'),
        default=False,
        db_index=True,
    )

    ACCEPTED_CHOICES = (
        (None, '----------'),
        (True, _('Accepted')),
        (False, _('Rejected')),
    )
    accepted = models.NullBooleanField(
        verbose_name=_('accepted'),
        default=None,
        choices=ACCEPTED_CHOICES,
        db_index=True,
    )

    additionalspeaker_set = GenericRelation(
        to=AdditionalSpeaker,
        content_type_field='proposal_type',
        object_id_field='proposal_id',
    )

    # Generic labels field
    labels = models.CharField(
        verbose_name=_('labels'),
        blank=True,
        max_length=128,
    )

    objects = DefaultConferenceManager.from_queryset(ProposalQuerySet)()
    all_objects = ProposalQuerySet.as_manager()

    _must_fill_fields = [
        'abstract', 'objective', 'supplementary',
        'detailed_description', 'outline',
    ]

    class Meta:
        abstract = True

    @property
    def speakers(self):
        yield PrimarySpeaker(proposal=self)

        # Optimization: Callers of this method can annotate the queryset to
        # avoid lookups when a proposal doesn't have any additional speakers.
        with contextlib.suppress(AttributeError):
            if self._additional_speaker_count < 1:
                return

        # Optimization: Callers of this method can prefetch the additional
        # speaker queryset to avoid n+1 lookups when operating on multiple
        # proposals. Example::
        #
        #   proposals = TalkProposal.objects.prefetch_related(Prefetch(
        #       'additionalspeaker_set',
        #       to_attr='_additional_speakers',
        #       queryset=(
        #           AdditionalSpeaker.objects
        #           .filter(cancelled=False)
        #           .select_related('user')
        #       ),
        #   ))
        #   for p in proposals:   # Only two queries: proposals, and speakers.
        #       for s in p.speakers:
        #           print(s.user.email)
        try:
            additionals = self._additional_speakers
        except AttributeError:
            additionals = (
                self.additionalspeaker_set
                .filter(cancelled=False)
                .select_related('user')
            )

        yield from additionals

    @property
    def speaker_count(self):
        # Optimization: Callers of this method can annotate the queryset to
        # avoid n+1 lookups when operating on multiple proposals.
        try:
            count = self._additional_speaker_count
        except AttributeError:
            count = self.additionalspeaker_set.filter(cancelled=False).count()
        return count + 1

    @property
    def must_fill_fields_count(self):
        return len(self._must_fill_fields)

    @property
    def finished_fields_count(self):
        count = sum(1 for f in self._must_fill_fields if getattr(self, f))
        return count

    @property
    def finish_percentage(self):
        return 100 * self.finished_fields_count // self.must_fill_fields_count

    @property
    def unfinished_fields_count(self):
        return self.must_fill_fields_count - self.finished_fields_count


class TalkProposal(AbstractProposal):

    duration = models.CharField(
        verbose_name=_('Duration'),
        max_length=6,
    )

    FIRST_SPEAKER_CHOICES = (
        (True, _('Yes, it is my first time speaking at PyCon Taiwan.')),
        (False, _('No, I have given talks at PyCon Taiwan in the past.'))
    )

    first_time_speaker = models.BooleanField(
        verbose_name=_('first time speaker'),
        default=False,
        choices=FIRST_SPEAKER_CHOICES)

    class Meta(AbstractProposal.Meta):
        verbose_name = _('talk proposal')
        verbose_name_plural = _('talk proposals')

    @property
    def duration_dict(self):
        if not hasattr(TalkProposal, '_duration_dict'):
            TalkProposal._duration_dict = dict(
                settings.TALK_PROPOSAL_DURATION_CHOICES,
            )
        return TalkProposal._duration_dict

    def get_peek_url(self):
        return reverse('talk_proposal_peek', kwargs={'pk': self.pk})

    def get_update_url(self):
        return reverse('talk_proposal_update', kwargs={'pk': self.pk})

    def get_cancel_url(self):
        return reverse('talk_proposal_cancel', kwargs={'pk': self.pk})

    def get_manage_speakers_url(self):
        return reverse('talk_proposal_manage_speakers', kwargs={'pk': self.pk})

    def get_remove_speaker_url(self, speaker):
        return reverse('talk_proposal_remove_speaker', kwargs={
            'pk': self.pk, 'email': speaker.user.email,
        })

    def get_duration_display(self):
        return self.duration_dict.get(self.duration)


class TutorialProposal(AbstractProposal):

    DURATION_CHOICES = (
        ('1.5hr', _('1.5hr')),
    )

    duration = models.CharField(
        verbose_name=_('Duration'),
        max_length=7,
        choices=DURATION_CHOICES,
        default='1.5hr',
    )

    class Meta(AbstractProposal.Meta):
        verbose_name = _('tutorial proposal')
        verbose_name_plural = _('tutorial proposals')

    def get_peek_url(self):
        return reverse('tutorial_proposal_peek', kwargs={'pk': self.pk})

    def get_update_url(self):
        return reverse('tutorial_proposal_update', kwargs={'pk': self.pk})

    def get_cancel_url(self):
        return reverse('tutorial_proposal_cancel', kwargs={'pk': self.pk})

    def get_manage_speakers_url(self):
        return reverse('tutorial_proposal_manage_speakers', kwargs={
            'pk': self.pk,
        })

    def get_remove_speaker_url(self, speaker):
        return reverse('tutorial_proposal_remove_speaker', kwargs={
            'pk': self.pk, 'email': speaker.user.email,
        })


class LLMReview(ConferenceRelated):
    """Model for storing AI-generated reviews of proposals."""

    STAGE_1 = 'S1'
    STAGE_2 = 'S2'
    REVIEW_STAGE_CHOICES = (
        (STAGE_1, _('Stage 1')),
        (STAGE_2, _('Stage 2')),
    )

    proposal = BigForeignKey(
        on_delete=models.CASCADE,
        related_name='llm_reviews',
        to='proposals.TalkProposal',
        verbose_name=_('proposal')
    )

    stage = models.CharField(
        verbose_name=_('review stage'),
        max_length=2,
        choices=REVIEW_STAGE_CHOICES,
        default=STAGE_1,
        db_index=True,
    )

    categories = ArrayField(
        models.CharField(max_length=5, choices=CATEGORY_CHOICES),
        size=3,
        verbose_name=_('Categories'),
        default=list,
        blank=True
    )

    summary = models.TextField(
        verbose_name=_('AI-generated summary'),
    )

    comment = models.TextField(
        verbose_name=_('AI-generated comment'),
    )

    translated_summary = models.TextField(
        verbose_name=_('translated summary'),
    )

    translated_comment = models.TextField(
        verbose_name=_('translated comment'),
    )

    VOTE_CHOICES = (
        ('-1', _('-1 (strong reject)')),
        ('-0', _('-0 (weak reject)')),
        ('+0', _('+0 (weak accept)')),
        ('+1', _('+1 (strong accept)')),
    )

    vote = models.CharField(
        verbose_name=_('AI vote'),
        max_length=2,
        choices=VOTE_CHOICES,
    )

    created_at = models.DateTimeField(
        verbose_name=_('created at'),
        auto_now_add=True,
    )

    stage_diff = models.TextField(
        verbose_name=_('stage difference'),
        blank=True,
        default=''
    )

    translated_stage_diff = models.TextField(
        verbose_name=_('translated stage difference'),
        blank=True,
        default=''
    )

    class Meta:
        verbose_name = _('LLM review')
        verbose_name_plural = _('LLM reviews')
        unique_together = (('proposal', 'stage'),)
        ordering = ['proposal__title', 'stage', '-created_at']

    def __str__(self):
        return f'AI Review for {self.proposal.title} ({self.get_stage_display()})'
