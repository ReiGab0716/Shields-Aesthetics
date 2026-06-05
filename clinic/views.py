from datetime import date, datetime, timedelta
import json

from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm, PasswordChangeForm
from django.contrib.auth.models import User
from django.contrib import messages
from django.conf import settings
from django.db import IntegrityError
from django.db.models import Q, Sum, Count
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify

from .models import Appointment, PatientProfileAudit, Profile, Service


PHONE_ERROR = "Enter a valid Philippine mobile number in the format 09XXXXXXXXX."
CLINIC_SLOTS = [
    "09:00:00", "09:30:00", "10:00:00", "10:30:00",
    "11:00:00", "11:30:00", "13:00:00", "13:30:00",
    "14:00:00", "14:30:00", "15:00:00", "15:30:00",
    "16:00:00", "16:30:00",
]


def is_valid_phone(phone):
    return not phone or (phone.isdigit() and len(phone) == 11 and phone.startswith("09"))


def extract_patient_id(value):
    digits = ''.join(ch for ch in (value or '') if ch.isdigit())
    return int(digits) if digits else None


def build_appointment_search_query(search_query):
    patient_id = extract_patient_id(search_query)
    query = (
        Q(patient__first_name__icontains=search_query) |
        Q(patient__last_name__icontains=search_query) |
        Q(patient__username__icontains=search_query) |
        Q(service__name__icontains=search_query) |
        Q(reference_number__icontains=search_query)
    )
    if patient_id:
        query |= Q(patient__id=patient_id)
    return query


def count_unique_trns(queryset):
    referenced = queryset.exclude(reference_number__isnull=True).exclude(reference_number="")
    missing_reference = queryset.filter(Q(reference_number__isnull=True) | Q(reference_number=""))
    return referenced.values("reference_number").distinct().count() + missing_reference.values("id").distinct().count()


def profile_refresh_response(request):
    if request.headers.get("HX-Request"):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("patient_profile")
        return response
    return redirect("patient_profile")


def redirect_to_next(request, fallback):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(fallback)


def parse_booking_datetime(appointment_date, appointment_time):
    for time_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"):
        try:
            naive_dt = datetime.strptime(f"{appointment_date} {appointment_time}", time_format)
            return timezone.make_aware(naive_dt, timezone.get_current_timezone())
        except ValueError:
            continue
    raise ValueError("Invalid date/time format.")


def validate_booking_datetime(appointment_date, appointment_time):
    booking_dt = parse_booking_datetime(appointment_date, appointment_time)
    now = timezone.localtime()
    local_booking_dt = timezone.localtime(booking_dt)
    if local_booking_dt.weekday() == 6:
        return "Sundays are rest days. Please choose another date."
    if local_booking_dt <= now + timedelta(hours=1):
        return "Appointments must be booked at least 1 hour in advance."
    return None


def staff_dashboard_route(user):
    role = getattr(getattr(user, "profile", None), "role", None)
    return {
        "DOCTOR": "doctor_dashboard",
        "SECRETARY": "secretary_dashboard",
        "PATIENT": "patient_dashboard",
    }.get(role, "home")


def homepage_appointment_target(user):
    if not user.is_authenticated:
        return "#contact"

    role = getattr(getattr(user, "profile", None), "role", None)
    if role == "PATIENT":
        return reverse("book_appointment")
    if role in ["DOCTOR", "SECRETARY"]:
        return reverse("schedule_appointment")
    return "#contact"


def normalized_no_email(email):
    return email if email else "No Email"


def build_patient_username(first_name, last_name):
    base = slugify(f"{first_name}.{last_name}".strip(".")) or "patient"
    base = base.replace("-", ".")
    username = base
    counter = 1
    while User.objects.filter(username=username).exists():
        counter += 1
        username = f"{base}{counter}"
    return username


def identity_conflict_error(first_name, last_name, email, phone, exclude_user_id=None):
    same_name = User.objects.filter(
        first_name__iexact=first_name,
        last_name__iexact=last_name,
        profile__role='PATIENT'
    )
    if exclude_user_id:
        same_name = same_name.exclude(id=exclude_user_id)

    if not same_name.exists():
        return None

    if not email and not phone:
        return "A patient with this name already exists. Add a unique email or phone number."

    contact_query = Q(pk__isnull=True)
    if email:
        contact_query |= Q(email__iexact=email)
    if phone:
        contact_query |= Q(profile__phone_number=phone)
    duplicate_contact = same_name.filter(contact_query)
    if duplicate_contact.exists():
        return "A patient with this name and contact information already exists."

    email_taken = email and User.objects.filter(email__iexact=email, profile__role='PATIENT').exclude(id=exclude_user_id).exists()
    phone_taken = phone and Profile.objects.filter(phone_number=phone, role='PATIENT').exclude(user_id=exclude_user_id).exists()
    if email_taken or phone_taken:
        return "Email or phone number is already used by another patient."

    return None

def home(request):
    all_services = Service.objects.all()
    return render(request, 'clinic/home.html', {
        'services': all_services,
        'appointment_target': homepage_appointment_target(request.user),
    })

def login_view(request):
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('login_redirect')
        else:
            context = {'login_error': 'Invalid username or password.'}
            return render(request, 'clinic/login.html', context)
    return render(request, 'clinic/login.html')

def register_view(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            first_name = request.POST.get('first_name', '').strip().title()
            last_name = request.POST.get('last_name', '').strip().title()
            phone = request.POST.get('phone', '').strip()
            email = request.POST.get('email', '').strip()
            if not is_valid_phone(phone):
                messages.error(request, PHONE_ERROR)
                return render(request, 'clinic/register.html', {'form': form})
            conflict = identity_conflict_error(first_name, last_name, email, phone)
            if conflict:
                messages.error(request, conflict)
                return render(request, 'clinic/register.html', {'form': form})

            user = form.save()
            user.first_name = first_name
            user.last_name = last_name
            if email:
                user.email = email
            user.save()
            
            profile = user.profile
            profile.phone_number = phone
            profile.role = 'PATIENT' 
            profile.save()
            
            login(request, user)
            return redirect('login_redirect')
    else:
        form = UserCreationForm()
    return render(request, 'clinic/register.html', {'form': form})

def logout_view(request):
    logout(request)
    request.session.flush()
    response = redirect('login')
    response["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    response["Clear-Site-Data"] = '"cache"'
    response.delete_cookie(settings.SESSION_COOKIE_NAME)
    return response

# --- ACCESS CONTROL & DASHBOARDS ---

@login_required
def login_redirect(request):
    if request.user.is_staff:
        return redirect('/admin/')

    role = getattr(request.user.profile, "role", None)

    return redirect(staff_dashboard_route(request.user))


@login_required
def patient_dashboard(request):
    if request.user.profile.role != 'PATIENT':
        return redirect('home')
    
    my_appointments = Appointment.objects.filter(patient=request.user).order_by('-date', '-time')
    total_visits = my_appointments.filter(status='Completed').count()
    
    next_appointment = Appointment.objects.filter(
        patient=request.user, 
        date__gte=timezone.now().date(),
        status__in=['Pending', 'Confirmed']
    ).order_by('date', 'time').first()
    
    recent_activity = my_appointments[:5]
    
    context = {
        'appointments': recent_activity,
        'next_appointment': next_appointment,
        'total_visits': total_visits,
    }
    return render(request, 'clinic/patient_dashboard.html', context)

@login_required
def book_appointment(request):
    """Handles how patients select their treatment, date, and time slot."""
    if request.method == "POST":
        service_id = request.POST.get('service_id')
        addon_id = request.POST.get('addon_id')
        appointment_date = request.POST.get('date')
        appointment_time = request.POST.get('time')
        notes = request.POST.get('skinConcerns', '')

        try:
            validation_error = validate_booking_datetime(appointment_date, appointment_time)
            if validation_error:
                return JsonResponse({
                    'status': 'error',
                    'message': validation_error
                }, status=400)
        except ValueError:
            return JsonResponse({
                'status': 'error',
                'message': "Invalid date/time format."
            }, status=400)

        if Appointment.objects.filter(
            patient=request.user,
            date=appointment_date,
            time=appointment_time
        ).exclude(status__iexact='Cancelled').exists():
            return JsonResponse({
                'status': 'error',
                'message': "You already booked this slot."
            }, status=400)

        try:
            service = Service.objects.get(id=service_id)

            total_price = service.price
            addon_obj = None

            if addon_id:
                addon_obj = Service.objects.filter(id=addon_id).first()
                if addon_obj:
                    total_price += addon_obj.price

            location = 'Baseline'
            if service.category == 'LASER':
                location = 'CebuDoc'
            
            appointment = Appointment.objects.create(
                patient=request.user,
                service=service,
                addon_service=addon_obj,
                has_addon=bool(addon_obj),
                date=appointment_date,
                time=appointment_time,
                notes=notes,
                status='Pending',
                specialist='Dr. Shields',
                location=location,
                price_at_booking=total_price
            )

            return JsonResponse({
                'status': 'success',
                'ref_code': appointment.reference_number
            })

        except IntegrityError:
            return JsonResponse({
                'status': 'error',
                'message': "Slot just got taken."
            }, status=400)

    # --- GET ---
    services = Service.objects.all()

    return render(request, 'clinic/patient_set_appointment.html', {
        'services': services,
        'facial_services': services.filter(category='FACIAL'),
        'zo_services': services.filter(category='ZO'),
        'doctor_services': services.filter(category='DOCTOR'),
        'laser_services': services.filter(category='LASER'),
        'addons': services.filter(category='ADDON'),
    })
# --- PATIENT SERVICES & PROFILE ---

@login_required
def patient_sessions(request):
    """Shows each patient their appointment history and cancellation options."""
    all_sessions = Appointment.objects.filter(patient=request.user).order_by('-date', '-time')
    total_visits = all_sessions.filter(status='Completed').count()
    pending_count = all_sessions.filter(status='Pending').count()
    
    context = {
        'base_template': "clinic/base_partial.html" if request.headers.get('HX-Request') else "clinic/base_patient.html",
        'sessions': all_sessions,
        'total_visits': total_visits,
        'pending_count': pending_count,
    }
    
    return render(request, 'clinic/patient_sessions.html', context)

@login_required
def patient_profile(request):
    """Lets patients and staff update their own profile, image, and password."""
    user = request.user
    profile = user.profile
    is_patient = profile.role == 'PATIENT'
    base_template = "clinic/base_patient.html" if is_patient else "clinic/base_staff.html"
    total_visits = Appointment.objects.filter(patient=user, status='Completed').count() if is_patient else Appointment.objects.filter(status='Completed').count()
    password_form = PasswordChangeForm(user)

    if request.method == "POST":
        if 'update_profile' in request.POST:
            old_username = user.username
            old_email = user.email
            old_phone = profile.phone_number or ""
            new_username = request.POST.get('username', user.username).strip()
            new_email = request.POST.get('email', '').strip()
            first_name = request.POST.get('first_name', '').strip()
            last_name = request.POST.get('last_name', '').strip()
            phone = request.POST.get('phone', '').strip()
            email_changed = new_email.lower() != (old_email or "").lower()
            phone_changed = phone != old_phone

            if User.objects.filter(username__iexact=new_username).exclude(id=user.id).exists():
                messages.error(request, "Username is already taken.")
            elif email_changed and new_email and User.objects.filter(email__iexact=new_email).exclude(id=user.id).exists():
                messages.error(request, "Email is already used by another account.")
            elif not is_valid_phone(phone):
                messages.error(request, PHONE_ERROR)
            else:
                conflict_email = new_email if email_changed else ""
                conflict_phone = phone if phone_changed else ""
                conflict = identity_conflict_error(first_name, last_name, conflict_email, conflict_phone, exclude_user_id=user.id) if is_patient else None
                if conflict:
                    messages.error(request, conflict)
                    context = {
                        'base_template': "clinic/base_partial.html" if request.headers.get('HX-Request') else base_template,
                        'user': user,
                        'profile': profile,
                        'password_form': password_form,
                        'total_visits': total_visits,
                    }
                    return render(request, 'clinic/patient_profile.html', context)

                user.username = new_username
                user.email = new_email
                user.first_name = first_name
                user.last_name = last_name
                profile.phone_number = phone
                if request.FILES.get('profile_image'):
                    profile.profile_image = request.FILES['profile_image']
                user.save()
                profile.save()

                if old_username != user.username:
                    PatientProfileAudit.objects.create(
                        patient=user,
                        changed_by=request.user,
                        field_name="username",
                        old_value=old_username,
                        new_value=user.username,
                    )
                if old_email != user.email:
                    PatientProfileAudit.objects.create(
                        patient=user,
                        changed_by=request.user,
                        field_name="email",
                        old_value=old_email,
                        new_value=user.email,
                    )
                messages.success(request, "Profile updated successfully!")
                return profile_refresh_response(request)
        elif 'change_password' in request.POST:
            password_form = PasswordChangeForm(user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Password updated successfully!")
                return profile_refresh_response(request)

    context = {
        'base_template': "clinic/base_partial.html" if request.headers.get('HX-Request') else base_template,
        'user': user,
        'profile': profile,
        'password_form': password_form,
        'total_visits': total_visits,
    }
    return render(request, 'clinic/patient_profile.html', context)

@login_required
def doctor_dashboard(request):
    """Summarizes appointments and revenue for the doctor dashboard."""
    if request.user.profile.role != "DOCTOR":
        return redirect("home")

    appointments = Appointment.objects.select_related(
        'patient', 'service', 'addon_service'
    ).order_by("-date", "-time")

    today = date.today()
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=7)
    month_start = today.replace(day=1)

    total_appointments = appointments.count()
    pending_count = appointments.filter(status__in=["Pending", "Confirmed"]).count()

    total_today = appointments.filter(date=today).count()
    total_week = appointments.filter(date__gte=week_start).count()
    total_month = appointments.filter(date__gte=month_start).count()

    completed = appointments.filter(status="Completed")

    total_sales = completed.aggregate(
        total=Sum('price_at_booking')
    )['total'] or 0

    monthly_sales = completed.filter(
        date__gte=month_start
    ).aggregate(total=Sum('price_at_booking'))['total'] or 0

    daily_sales = completed.filter(
        date=today
    ).aggregate(total=Sum('price_at_booking'))['total'] or 0

    completed_count = completed.count()

    context = {
        "appointments": appointments[:20],
        "today_appointments": appointments.filter(date=today).order_by("time")[:8],
        "tomorrow_appointments": appointments.filter(date=tomorrow).order_by("time")[:8],

        "total_appointments": total_appointments,
        "pending_count": pending_count,
        "total_today": total_today,
        "total_week": total_week,
        "total_month": total_month,

        "total_sales": total_sales,
        "monthly_sales": monthly_sales,
        "daily_sales": daily_sales,
        "completed_count": completed_count,
    }

    return render(request, "clinic/doctor_dashboard.html", context)


@login_required
def change_user_role(request, user_id):

    if request.user.profile.role != "DOCTOR":
        return redirect("home")

    profile = get_object_or_404(Profile, user_id=user_id)

    if request.method == "POST":
        new_role = request.POST.get("role")

        if new_role in ["PATIENT", "SECRETARY", "DOCTOR"]:
            profile.role = new_role
            profile.save()

    return redirect_to_next(request, "doctor_manage_users")

@login_required
def doctor_set_appointment(request):
    if request.user.profile.role != "DOCTOR":
        return redirect("home")

    return redirect("schedule_appointment")

@login_required
def doctor_manage_sessions(request):
    """Filters and manages all sessions available to doctors."""
    if request.user.profile.role != "DOCTOR":
        return redirect("home")

    status = request.GET.get("status", "all").lower()
    search_query = request.GET.get("search", "")
    selected_date = request.GET.get("date", "")

    sessions = Appointment.objects.select_related('patient', 'service')

    if status == "active":
        sessions = sessions.filter(status__in=["Pending", "Confirmed"])
    elif status != "all":
        sessions = sessions.filter(status__iexact=status)

    if selected_date:
        try:
            filter_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
            sessions = sessions.filter(date=filter_date)
        except ValueError:
            selected_date = ""

    if search_query:
        sessions = sessions.filter(build_appointment_search_query(search_query))

    sessions = sessions.order_by("-id")

    today = datetime.now().date()
    min_date = today
    max_date = today + timedelta(days=365)

    completed_sessions = Appointment.objects.filter(status="Completed")

    return render(request, "clinic/doctor_manage_sessions.html", {
        "sessions": sessions,
        "status": status,
        "search": search_query,
        "selected_date": selected_date,
        "min_date": min_date.isoformat(),
        "max_date": max_date.isoformat(),
        "total_sessions": count_unique_trns(completed_sessions),
        "total_visits": count_unique_trns(completed_sessions)
    })

@login_required
def doctor_sales(request):
    if request.user.profile.role != "DOCTOR":
        return redirect("home")

    today = timezone.now().date()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)
    completed = Appointment.objects.filter(status="Completed")

    total_sales = completed.aggregate(total=Sum("price_at_booking"))["total"] or 0
    weekly_sales = completed.filter(date__gte=week_start).aggregate(total=Sum("price_at_booking"))["total"] or 0
    monthly_sales = completed.filter(date__gte=month_start).aggregate(total=Sum("price_at_booking"))["total"] or 0
    daily_sales = completed.filter(date=today).aggregate(total=Sum("price_at_booking"))["total"] or 0
    completed_count = completed.count()

    service_breakdown = list(
        completed.values("service__name")
        .annotate(total=Sum("price_at_booking"), count=Count("id"))
        .order_by("-total")[:8]
    )
    top_treatments = service_breakdown[:5]

    trend_labels = []
    trend_values = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        trend_labels.append(day.strftime("%b %d"))
        trend_values.append(float(completed.filter(date=day).aggregate(total=Sum("price_at_booking"))["total"] or 0))

    popularity_labels = [item["service__name"] or "Unknown" for item in service_breakdown]
    popularity_values = [item["count"] for item in service_breakdown]

    return render(request, "clinic/doctor_sales.html", {
        "total_sales": total_sales,
        "weekly_sales": weekly_sales,
        "monthly_sales": monthly_sales,
        "daily_sales": daily_sales,
        "completed_count": completed_count,
        "service_breakdown": service_breakdown,
        "top_treatments": top_treatments,
        "trend_labels": json.dumps(trend_labels),
        "trend_values": json.dumps(trend_values),
        "popularity_labels": json.dumps(popularity_labels),
        "popularity_values": json.dumps(popularity_values),
    })
    
@login_required
def check_availability(request):
    """Returns unavailable times for a date, excluding cancelled appointments."""
    selected_date = request.GET.get('date')
    if selected_date:
        try:
            selected_day = datetime.strptime(selected_date, "%Y-%m-%d").date()
            if selected_day.weekday() == 6:
                return JsonResponse({'booked_times': CLINIC_SLOTS, 'is_rest_day': True})
        except ValueError:
            pass

        booked_slots = Appointment.objects.filter(
            date=selected_date
        ).exclude(status__iexact='Cancelled').values_list('time', flat=True)

        unavailable_times = {t.strftime('%H:%M:%S') for t in booked_slots}

        try:
            selected_day = datetime.strptime(selected_date, "%Y-%m-%d").date()
            now = timezone.localtime()
            if selected_day == now.date():
                cutoff = now + timedelta(hours=1)
                for slot in CLINIC_SLOTS:
                    slot_time = datetime.strptime(slot, "%H:%M:%S").time()
                    slot_dt = timezone.make_aware(datetime.combine(selected_day, slot_time), now.tzinfo)
                    if slot_dt <= cutoff:
                        unavailable_times.add(slot)
        except ValueError:
            pass

        booked_times = sorted(unavailable_times)
        return JsonResponse({'booked_times': booked_times})
    return JsonResponse({'booked_times': []})



def cancel_appointment_core(appointment):
    if appointment.status.strip().title() in ['Pending', 'Confirmed']:
        appointment.status = 'Cancelled'
        appointment.save()
        return True
    return False

@login_required
def cancel_appointment(request, appointment_id):
    appointment = get_object_or_404(
        Appointment,
        id=appointment_id,
        patient=request.user
    )

    if cancel_appointment_core(appointment):
        messages.success(
            request,
            f"Appointment {appointment.reference_number} has been cancelled."
        )
    else:
        messages.error(request, "This appointment cannot be cancelled.")

    return redirect_to_next(request, 'patient_sessions')

@login_required
def doctor_cancel_appointment(request, appointment_id):
    if request.user.profile.role != "DOCTOR":
        return redirect("home")

    appointment = get_object_or_404(Appointment, id=appointment_id)

    if cancel_appointment_core(appointment):
        messages.success(
            request,
            f"Appointment {appointment.reference_number} cancelled by doctor."
        )
    else:
        messages.error(request, "This appointment cannot be cancelled.")

    return redirect_to_next(request, 'doctor_manage_sessions')


@login_required
def doctor_complete_appointment(request, appointment_id):
    if request.user.profile.role != "DOCTOR":
        return redirect("home")

    appointment = get_object_or_404(Appointment, id=appointment_id)

    if appointment.status.strip().title() in ["Pending", "Confirmed"]:
        appointment.status = "Completed"
        appointment.save()
        messages.success(request, f"Appointment {appointment.reference_number} marked as completed.")
    else:
        messages.error(request, "This appointment cannot be marked as completed.")

    return redirect_to_next(request, "doctor_manage_sessions")


@login_required
def doctor_confirm_appointment(request, appointment_id):
    """Convert Pending appointment to Confirmed status"""
    if request.user.profile.role != "DOCTOR":
        return redirect("home")

    appointment = get_object_or_404(Appointment, id=appointment_id)

    if appointment.status.strip().title() == "Pending":
        appointment.status = "Confirmed"
        appointment.save()
        messages.success(request, f"Appointment {appointment.reference_number} confirmed.")
    else:
        messages.error(request, "Only pending appointments can be confirmed.")

    return redirect_to_next(request, "doctor_manage_sessions")
    
    
    
    

@login_required
def secretary_dashboard(request):
    """Summarizes daily and upcoming clinic work for secretaries."""
    if request.user.profile.role != "SECRETARY":
        return redirect("home")

    today = date.today()
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=7)

    appointments = Appointment.objects.select_related("patient", "service", "addon_service")

    context = {
        "total_appointments": appointments.count(),
        "pending_count": appointments.filter(status__in=["Pending", "Confirmed"]).count(),
        "total_today": appointments.filter(date=today).count(),
        "total_week": appointments.filter(date__gte=week_start).count(),
        "completed_count": appointments.filter(status="Completed").count(),
        "today_appointments": appointments.filter(date=today).order_by("time")[:8],
        "tomorrow_appointments": appointments.filter(date=tomorrow).order_by("time")[:8],
    }

    return render(request, "clinic/secretary_dashboard.html", context)


@login_required
def secretary_set_appointment(request):
    if request.user.profile.role != "SECRETARY":
        return redirect("home")

    return redirect("schedule_appointment")


@login_required
def secretary_manage_sessions(request):
    """Filters and manages all sessions available to secretaries."""
    if request.user.profile.role != "SECRETARY":
        return redirect("home")

    status = request.GET.get("status", "all").lower()
    search_query = request.GET.get("search", "")
    selected_date = request.GET.get("date", "")

    sessions = Appointment.objects.select_related('patient', 'service')

    if status == "active":
        sessions = sessions.filter(status__in=["Pending", "Confirmed"])
    elif status != "all":
        sessions = sessions.filter(status__iexact=status)

    if selected_date:
        try:
            filter_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
            sessions = sessions.filter(date=filter_date)
        except ValueError:
            selected_date = ""

    if search_query:
        sessions = sessions.filter(build_appointment_search_query(search_query))

    sessions = sessions.order_by("-id")

    today = datetime.now().date()
    min_date = today
    max_date = today + timedelta(days=365)

    completed_sessions = Appointment.objects.filter(status="Completed")

    return render(request, "clinic/secretary_manage_sessions.html", {
        "sessions": sessions,
        "status": status,
        "search": search_query,
        "selected_date": selected_date,
        "min_date": min_date.isoformat(),
        "max_date": max_date.isoformat(),
        "total_visits": count_unique_trns(completed_sessions),
        "total_sessions": count_unique_trns(completed_sessions)
    })
    
@login_required
def secretary_confirm_appointment(request, pk):
    """Convert Pending appointment to Confirmed status for secretary"""
    if request.user.profile.role != "SECRETARY":
        return HttpResponseForbidden("Not allowed")

    appointment = get_object_or_404(Appointment, id=pk)

    if request.method == "POST":
        if appointment.status.strip().title() == "Pending":
            appointment.status = "Confirmed"
            appointment.save()
            messages.success(request, f"Appointment {appointment.reference_number} confirmed.")
        else:
            messages.error(request, "Only pending appointments can be confirmed.")

    return redirect_to_next(request, "secretary_manage_sessions")
    
@login_required
def secretary_cancel_appointment(request, pk):
    if request.user.profile.role != "SECRETARY":
        return HttpResponseForbidden()

    appointment = get_object_or_404(Appointment, id=pk)
    
    if request.method == "POST":
        appointment.status = "Cancelled"
        appointment.save()
        messages.warning(request, f"Appointment {appointment.reference_number} has been Cancelled.")
    
    return redirect_to_next(request, "secretary_manage_sessions")

@login_required
def secretary_complete_appointment(request, pk):
    if request.user.profile.role != "SECRETARY":
        return HttpResponseForbidden()

    appointment = get_object_or_404(Appointment, id=pk)
    
    if request.method == "POST":
        appointment.status = "Completed"
        appointment.save()
        messages.success(request, f"Appointment {appointment.reference_number} marked as Completed.")
    
    return redirect_to_next(request, "secretary_manage_sessions")

@login_required
def doctor_manage_users(request):
    if request.user.profile.role != "DOCTOR":
        return redirect("home")

    role_filter = request.GET.get("role", "ALL")
    search_query = request.GET.get("search", "").strip()

    profiles = Profile.objects.select_related("user").all()

    # ROLE FILTER
    if role_filter != "ALL":
        profiles = profiles.filter(role=role_filter)

    # SEARCH FILTER
    if search_query:
        user_id = extract_patient_id(search_query)
        profiles = profiles.filter(
            Q(user__username__icontains=search_query) |
            Q(user__first_name__icontains=search_query) |
            Q(user__last_name__icontains=search_query) |
            Q(employee_id__icontains=search_query) |
            Q(doctor_id__icontains=search_query) |
            Q(user__id=user_id if user_id else -1)
        )

    return render(request, "clinic/doctor_manage_users.html", {
        "profiles": profiles,
        "role_filter": role_filter,
        "search_query": search_query
    })

@login_required
def get_patient_details(request, patient_id):
    """Get patient details for modal display"""
    try:
        patient = User.objects.get(id=patient_id)
        profile = patient.profile
        appointment_id = request.GET.get("appointment_id")
        appointment = None
        if appointment_id:
            appointment = Appointment.objects.filter(id=appointment_id, patient=patient).first()
        
        # Get appointment count
        appointment_count = Appointment.objects.filter(
            patient=patient
        ).exclude(status='Cancelled').count()
        
        # Get total spent
        total_spent = Appointment.objects.filter(
            patient=patient,
            status='Completed'
        ).aggregate(
            total=Sum('price_at_booking')
        )['total'] or 0
        
        data = {
            'patient_id': patient.id,
            'username': patient.username,
            'first_name': patient.first_name,
            'last_name': patient.last_name,
            'email': normalized_no_email(patient.email),
            'phone_number': profile.phone_number or 'No Phone',
            'profile_image_url': profile.profile_image.url if profile.profile_image else '',
            'total_appointments': appointment_count,
            'total_spent': float(total_spent),
            'date_joined': patient.date_joined.strftime('%Y-%m-%d'),
            'patient_notes': appointment.notes if appointment and appointment.notes else 'No patient notes recorded.',
        }
        
        return JsonResponse(data)
    except User.DoesNotExist:
        return JsonResponse({'error': 'Patient not found'}, status=404)


@login_required
def notification_poll(request):
    if request.user.profile.role not in ["DOCTOR", "SECRETARY"]:
        return JsonResponse({"count": 0, "latest_id": None, "latest_ref": None})

    pending_appointments = Appointment.objects.filter(status="Pending")
    latest = pending_appointments.order_by("-id").first()
    count = count_unique_trns(pending_appointments)
    return JsonResponse({
        "count": count,
        "latest_id": latest.id if latest else None,
        "latest_ref": latest.reference_number if latest else None,
    })
    
@login_required
def save_doctor_notes(request, appointment_id):
    """Save doctor notes (only for doctors)"""
    if request.user.profile.role != "DOCTOR":
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method != "POST":
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        appointment = get_object_or_404(Appointment, id=appointment_id)
        notes = request.POST.get('notes', '').strip()
        
        appointment.doctor_notes = notes
        appointment.save()
        
        return JsonResponse({
            'status': 'success',
            'message': 'Notes saved successfully',
            'updated_at': appointment.doctor_notes_updated_at.strftime('%Y-%m-%d %H:%M:%S')
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
def get_doctor_notes(request, appointment_id):
    """Get doctor notes (only for doctors)"""
    if request.user.profile.role != "DOCTOR":
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    try:
        appointment = get_object_or_404(Appointment, id=appointment_id)
        
        return JsonResponse({
            'notes': appointment.doctor_notes or '',
            'updated_at': appointment.doctor_notes_updated_at.strftime('%Y-%m-%d %H:%M:%S') if appointment.doctor_notes_updated_at else ''
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

@login_required
def patient_detail(request, patient_id):
    """View patient details and appointment history (Doctor & Secretary only)"""
    if request.user.profile.role not in ["DOCTOR", "SECRETARY"]:
        return redirect("home")
    
    patient = get_object_or_404(User, id=patient_id, profile__role="PATIENT")
    
    # Get all appointments for this patient
    appointments = Appointment.objects.filter(
        patient=patient
    ).select_related('service', 'addon_service').order_by('-date', '-time')
    show_all_history = request.GET.get("history") == "all"
    visible_appointments = appointments if show_all_history else appointments[:10]
    
    # Get statistics
    total_appointments = appointments.count()
    completed_appointments = appointments.filter(status='Completed').count()
    pending_appointments = appointments.filter(status='Pending').count()
    confirmed_appointments = appointments.filter(status='Confirmed').count()
    cancelled_appointments = appointments.filter(status='Cancelled').count()
    
    total_spent = appointments.filter(
        status='Completed'
    ).aggregate(
        total=Sum('price_at_booking')
    )['total'] or 0
    
    # Get upcoming appointments
    today = datetime.now().date()
    upcoming_appointments = appointments.filter(
        date__gte=today,
        status__in=['Pending', 'Confirmed']
    ).order_by('date', 'time')
    
    return render(request, "clinic/patient_detail.html", {
        "patient": patient,
        "appointments": visible_appointments,
        "show_all_history": show_all_history,
        "has_more_history": total_appointments > 10,
        "upcoming_appointments": upcoming_appointments,
        "total_appointments": total_appointments,
        "completed_appointments": completed_appointments,
        "pending_appointments": pending_appointments,
        "confirmed_appointments": confirmed_appointments,
        "cancelled_appointments": cancelled_appointments,
        "total_spent": total_spent
    })

@login_required
def all_patients(request):
    """View all patients in the system (Doctor & Secretary only)"""
    if request.user.profile.role not in ["DOCTOR", "SECRETARY"]:
        return redirect("home")
    
    # Get all patients (users with PATIENT role)
    patients = User.objects.filter(
        profile__role="PATIENT"
    ).select_related('profile').order_by('first_name', 'last_name')
    
    # Optional: Search filter
    search_query = request.GET.get("search", "").strip()
    if search_query:
        patient_id = extract_patient_id(search_query)
        patients = patients.filter(
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(username__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(id=patient_id if patient_id else -1)
        )
    
    # Add appointment count to each patient
    patients_with_counts = []
    for patient in patients:
        appointment_count = Appointment.objects.filter(
            patient=patient
        ).exclude(status='Cancelled').count()
        
        total_spent = Appointment.objects.filter(
            patient=patient,
            status='Completed'
        ).aggregate(
            total=Sum('price_at_booking')
        )['total'] or 0
        
        patients_with_counts.append({
            'user': patient,
            'appointment_count': appointment_count,
            'total_spent': total_spent
        })
    
    return render(request, "clinic/all_patients.html", {
        "patients": patients_with_counts,
        "search_query": search_query,
        "total_patients": len(patients_with_counts)
    })

@login_required
def search_patients(request):
    """API endpoint for searching patients"""
    if request.user.profile.role not in ["DOCTOR", "SECRETARY"]:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    query = request.GET.get('q', '').strip()
    
    if not query or len(query) < 2:
        return JsonResponse({'patients': []})
    
    patient_id = extract_patient_id(query)
    # Search by name, email, phone, or patient ID
    patients = User.objects.filter(
        profile__role='PATIENT'
    ).filter(
        Q(first_name__icontains=query) |
        Q(last_name__icontains=query) |
        Q(email__icontains=query) |
        Q(username__icontains=query) |
        Q(profile__phone_number__icontains=query) |
        Q(id=patient_id if patient_id else -1)
    ).values(
        'id',
        'username',
        'first_name',
        'last_name',
        'email',
        'profile__phone_number'
    ).order_by('first_name')[:10]
    
    return JsonResponse({'patients': list(patients)})

@login_required
def quick_register_patient(request):
    """Quick register new patient"""
    if request.user.profile.role not in ["DOCTOR", "SECRETARY"]:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        name = request.POST.get('name', '').strip()
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip()
        
        # Validate
        if not name or not phone:
            return JsonResponse({
                'error': 'Name and phone are required'
            }, status=400)
        if not is_valid_phone(phone):
            return JsonResponse({'error': PHONE_ERROR}, status=400)
        
        if len(name) < 2:
            return JsonResponse({
                'error': 'Name must be at least 2 characters'
            }, status=400)
        
        # Create user
        parts = name.split(' ', 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""
        conflict = identity_conflict_error(first_name, last_name, email, phone)
        if conflict:
            return JsonResponse({'error': conflict}, status=400)

        username = build_patient_username(first_name, last_name)
        temporary_password = get_random_string(10)
        
        user = User.objects.create_user(
            username=username,
            email=email or f"{username}@clinic.local",
            first_name=first_name,
            last_name=last_name,
            password=temporary_password
        )
        
        # Create profile (automatically created by signal, but ensure)
        user.profile.phone_number = phone
        user.profile.role = 'PATIENT'
        user.profile.save()
        
        return JsonResponse({
            'status': 'success',
            'patient': {
                'id': user.id,
                'username': user.username,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'email': user.email,
                'phone': user.profile.phone_number,
                'temporary_password': temporary_password
            }
        })
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)
    
@login_required
def create_appointment(request):
    """Creates staff-booked appointments and returns credentials for new patients."""
    if request.user.profile.role not in ["DOCTOR", "SECRETARY"]:
        return redirect('home')
    
    if request.method == 'POST':
        try:
            patient_id = request.POST.get('patient_id')
            service_id = request.POST.get('service_id')
            addon_id = request.POST.get('addon_id')
            appointment_date = request.POST.get('date')
            appointment_time = request.POST.get('time')
            notes = request.POST.get('notes', '')
            
            patient = get_object_or_404(User, id=patient_id, profile__role='PATIENT')
            service = get_object_or_404(Service, id=service_id)
            
            try:
                validation_error = validate_booking_datetime(appointment_date, appointment_time)
                if validation_error:
                    return JsonResponse({'error': validation_error}, status=400)
            except ValueError:
                return JsonResponse({
                    'error': 'Invalid date/time format'
                }, status=400)
            
            if Appointment.objects.filter(
                date=appointment_date,
                time=appointment_time,
                status__in=['Pending', 'Confirmed']
            ).exclude(status='Cancelled').exists():
                return JsonResponse({
                    'error': 'Time slot already booked. Choose another time.'
                }, status=400)
            
            total_price = service.price
            addon_obj = None
            if addon_id:
                addon_obj = Service.objects.filter(id=addon_id).first()
                if addon_obj:
                    total_price += addon_obj.price
            
            location = 'Baseline'
            if service.category == 'LASER':
                location = 'CebuDoc'
            
            appointment = Appointment.objects.create(
                patient=patient,
                service=service,
                addon_service=addon_obj,
                has_addon=bool(addon_obj),
                date=appointment_date,
                time=appointment_time,
                notes=notes,
                status='Pending',
                location=location,
                price_at_booking=total_price,
                specialist='Dr. Shields'
            )
            
            return JsonResponse({
                'status': 'success',
                'message': f'Appointment created successfully',
                'reference_number': appointment.reference_number,
                'patient_username': request.POST.get('patient_username', ''),
                'temporary_password': request.POST.get('temporary_password', ''),
                'redirect_url': reverse('secretary_manage_sessions') if request.user.profile.role == 'SECRETARY' else reverse('doctor_manage_sessions')
            })
            
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    
    # GET request - show booking form
    services = Service.objects.all()
    
    return render(request, 'clinic/schedule_appointment.html', {
        'services': services,
        'facial_services': services.filter(category='FACIAL'),
        'zo_services': services.filter(category='ZO'),
        'doctor_services': services.filter(category='DOCTOR'),
        'laser_services': services.filter(category='LASER'),
        'addon_services': services.filter(category='ADDON')
    })

@login_required
def get_service_details(request, service_id):
    """Get service details (price, duration, etc)"""
    if request.user.profile.role not in ["DOCTOR", "SECRETARY"]:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    service = get_object_or_404(Service, id=service_id)
    
    return JsonResponse({
        'id': service.id,
        'name': service.name,
        'category': service.category,
        'price': float(service.price),
        'description': service.description,
        'image_url': service.image.url if service.image else '',
        'estimated_duration': 60
    })
