import re

from django.utils import six
from django.forms.forms import BoundField, DeclarativeFieldsMetaclass, Form
from django.core.exceptions import ValidationError
from django.forms.fields import FileField

from piston.forms import RestFormMixin

from is_core.forms.fields import SmartReadonlyField
from is_core.forms.widgets import SmartWidgetMixin


def pretty_class_name(class_name):
    return re.sub(r'(\w)([A-Z])', r'\1-\2', class_name).lower()


class SmartBoundField(BoundField):

    is_readonly = False

    def as_widget(self, widget=None, attrs=None, only_initial=False):
        """
        Renders the field by rendering the passed widget, adding any HTML
        attributes passed as attrs.  If no widget is specified, then the
        field's default widget will be used.
        """
        if not widget:
            widget = self.field.widget

        if self.field.localize:
            widget.is_localized = True

        attrs = attrs or {}
        auto_id = self.auto_id
        if auto_id and 'id' not in attrs and 'id' not in widget.attrs:
            if not only_initial:
                attrs['id'] = auto_id
            else:
                attrs['id'] = self.html_initial_id

        if not only_initial:
            name = self.html_name
        else:
            name = self.html_initial_name

        if isinstance(widget, SmartWidgetMixin):
            return widget.smart_render(self.form._request, name, self.value(), attrs=attrs)
        return widget.render(name, self.value(), attrs=attrs)

    @property
    def type(self):
        if self.is_readonly:
            return 'readonly'
        else:
            return pretty_class_name(self.field.widget.__class__.__name__)


class ReadonlyBoundField(SmartBoundField):

    is_readonly = True

    def __init__(self, form, field, name):
        if isinstance(field, SmartReadonlyField):
            field._set_val_and_label(form.instance)
        super(ReadonlyBoundField, self).__init__(form, field, name)

    def as_widget(self, widget=None, attrs=None, only_initial=False):
        if not widget:
            widget = self.field.widget
        return super(ReadonlyBoundField, self).as_widget(self.form._get_readonly_widget(self.name, self.field,
                                                                                        widget), attrs, only_initial)

    def value(self):
        data = self.form.initial.get(self.name, self.field.initial)
        if callable(data):
            data = data()
        return self.field.prepare_value(data)


class SmartFormMetaclass(DeclarativeFieldsMetaclass):

    def __new__(cls, name, bases,
                attrs):
        new_class = super(SmartFormMetaclass, cls).__new__(cls, name, bases,
                attrs)

        opts = getattr(new_class, 'Meta', None)
        if opts:
            exclude_fields = getattr(opts, 'exclude', None) or ()
            readonly_fields = getattr(opts, 'readonly_fields', None) or ()
            readonly = getattr(opts, 'readonly', None) or False

            base_readonly_fields = []
            for name, field in new_class.base_fields.items():
                if name in exclude_fields:
                    del new_class.base_fields[name]
                elif name in readonly_fields or readonly or field.is_readonly:
                    base_readonly_fields.append(name)

            for field_name in set(readonly_fields):
                if field_name not in new_class.base_fields and 'formreadonlyfield_callback' in attrs:
                    new_class.base_fields[field_name] = attrs['formreadonlyfield_callback'](field_name)
                    base_readonly_fields.append(field_name)

            new_class.base_readonly_fields = base_readonly_fields
        return new_class


class SmartFormMixin(object):

    def __init__(self, *args, **kwargs):
        super(SmartFormMixin, self).__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if hasattr(self, '_init_%s' % field_name):
                getattr(self, '_init_%s' % field_name)(field)
        self._init_fields()

    def _init_fields(self):
        pass

    def __getitem__(self, name):
        "Returns a BoundField with the given name."
        try:
            field = self.fields[name]
        except KeyError:
            raise KeyError('Key %r not found in Form' % name)

        if name in self.base_readonly_fields:
            return ReadonlyBoundField(self, field, name)
        else:
            return SmartBoundField(self, field, name)

    def _get_readonly_widget(self, field_name, field, widget):
        if field.readonly_widget:
            return field.readonly_widget(widget)
        return field.widget

    def _clean_fields(self):
        for name, field in self.fields.items():
            if name not in self.base_readonly_fields:
                # value_from_datadict() gets the data from the data dictionaries.
                # Each widget type knows how to retrieve its own data, because some
                # widgets split data over several HTML fields.
                value = field.widget.value_from_datadict(self.data, self.files, self.add_prefix(name))
                try:
                    if isinstance(field, FileField):
                        initial = self.initial.get(name, field.initial)
                        value = field.clean(value, initial)
                    else:
                        value = field.clean(value)
                    self.cleaned_data[name] = value
                    if hasattr(self, 'clean_%s' % name):
                        value = getattr(self, 'clean_%s' % name)()
                        self.cleaned_data[name] = value
                except ValidationError as e:
                    self._errors[name] = self.error_class(e.messages)
                    if name in self.cleaned_data:
                        del self.cleaned_data[name]

    def _set_readonly(self, field_name):
        if field_name not in self.base_readonly_fields:
            self.base_readonly_fields.append(field_name)


class SmartForm(six.with_metaclass(SmartFormMetaclass, SmartFormMixin), RestFormMixin, Form):
    pass


def smartform_factory(request, form, readonly_fields=None, exclude=None, formreadonlyfield_callback=None,
                      readonly=False):
    attrs = {}
    if exclude is not None:
        attrs['exclude'] = exclude
    if readonly_fields is not None:
        attrs['readonly_fields'] = readonly_fields
    attrs['readonly'] = readonly
    # If parent form class already has an inner Meta, the Meta we're
    # creating needs to inherit from the parent's inner meta.
    parent = (object,)
    if hasattr(form, 'Meta'):
        parent = (form.Meta, object)
    Meta = type(str('Meta'), parent, attrs)

    class_name = form.__name__

    form_class_attrs = {
        'Meta': Meta,
        'formreadonlyfield_callback': formreadonlyfield_callback,
        '_request': request,
    }

    form_class = type(form)(class_name, (form,), form_class_attrs)
    return form_class