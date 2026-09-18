from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Allow dict[variable_key] lookups in templates: {{ mydict|get_item:key }}"""
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None
