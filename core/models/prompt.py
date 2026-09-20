"""Prompt templates — editable via the curation UI."""
from django.db import models


class PromptTemplate(models.Model):
    key           = models.CharField(max_length=100, db_index=True)
    label         = models.CharField(max_length=200)
    description   = models.TextField(blank=True)
    system_prompt = models.TextField()
    version       = models.PositiveIntegerField(default=1)
    is_active     = models.BooleanField(default=False, db_index=True)
    notes         = models.TextField(blank=True, help_text='Reason for this version / what changed')
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['key', '-version']

    def __str__(self):
        active = ' [active]' if self.is_active else ''
        return f'{self.key} v{self.version}{active}'

    def activate(self):
        """Deactivate all other versions of this key, then activate this one."""
        PromptTemplate.objects.filter(key=self.key, is_active=True).update(is_active=False)
        self.is_active = True
        self.save(update_fields=['is_active'])
