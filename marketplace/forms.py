from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User

from .models import Part


class RegisterForm(UserCreationForm):
    ROLE_CHOICES = [
        ("buyer", "Buyer"),
        ("seller", "Seller"),
    ]

    email = forms.EmailField(required=True)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    role = forms.ChoiceField(choices=ROLE_CHOICES, required=True)
    company_name = forms.CharField(max_length=255, required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "first_name", "last_name", "role", "company_name")


class LoginForm(AuthenticationForm):
    username = forms.CharField(label="Username or email")


class CheckoutForm(forms.Form):
    customer_name = forms.CharField(max_length=180, label="Имя и компания")
    customer_email = forms.EmailField(label="Email")
    customer_phone = forms.CharField(max_length=50, label="Телефон")
    delivery_address = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="Адрес доставки")


class SellerPartForm(forms.ModelForm):
    class Meta:
        model = Part
        fields = [
            "title",
            "oem_number",
            "description",
            "price",
            "currency",
            "incoterm",
            "moq",
            "availability",
            "availability_status",
            "backorder_allowed",
            "stock_quantity",
            "production_lead_days",
            "prep_to_ship_days",
            "shipping_lead_days",
            "gross_weight_kg",
            "length_cm",
            "width_cm",
            "height_cm",
            "country_of_origin",
            "cross_numbers",
            "mapping_status",
            "supplier_part_uid",
            "condition",
            "brand",
            "image_url",
            "category",
            "is_active",
        ]


class SellerBulkUploadForm(forms.Form):
    file = forms.FileField(label="CSV file")
    category = forms.CharField(max_length=120, required=False, initial="Epiroc")
    default_stock = forms.IntegerField(min_value=0, initial=20)


class RFQCreateForm(forms.Form):
    customer_name = forms.CharField(max_length=180, label="Контактное лицо")
    customer_email = forms.EmailField(label="Email")
    company_name = forms.CharField(max_length=255, required=False, label="Компания")
    mode = forms.ChoiceField(
        label="Режим подбора",
        choices=[
            ("auto", "AUTO"),
            ("semi", "SEMI"),
            ("manual_oem", "MANUAL OEM"),
        ],
        initial="semi",
    )
    urgency = forms.ChoiceField(
        label="Срочность",
        choices=[
            ("standard", "Standard"),
            ("urgent", "Urgent"),
            ("critical", "Critical"),
        ],
        initial="standard",
    )
    items_text = forms.CharField(
        label="Позиции запроса",
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": "Каждая строка: номер/запрос;количество\nПример:\nRE48786;2\nMAIN SWITCH;1",
            }
        ),
        help_text="Формат строки: Запрос;Количество. Если количество не указано, будет 1.",
    )
    notes = forms.CharField(
        label="Комментарий",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
