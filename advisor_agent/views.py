import time
import base64
import datetime
from django.utils import timezone
import jwt
from django.shortcuts import render, redirect
from django.conf import settings
from django.http import JsonResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from .utils import create_google_calendar_event
import requests
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.urls import reverse
from .models import HubspotIntegration
from datetime import datetime, timedelta
from django.contrib.auth import get_user_model, login
from .forms import ContactForm, NoteForm
import html2text
from .vectorstore import add_documents_to_vectorstore
from google.auth.exceptions import RefreshError
from .models import GmailPollingState
from .utils import fetch_gmail_messages, fetch_calendar_events, fetch_hubspot_contacts_and_notes
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
import json
from .models import GmailPollingState
from .utils import fetch_calendar_events
from .vectorstore import add_documents_to_vectorstore
from .agent import agent_respond
from .tools import get_ongoing_instructions
from .utils import create_hubspot_contact, create_hubspot_note

# Create your views here.

def chat_view(request):
    is_logged_in = 'google_credentials' in request.session
    user_name = None
    user_email = None
    hubspot_connected = False
    if is_logged_in:
        creds = request.session.get('google_credentials', {})
        user_name = creds.get('user_name')
        user_email = creds.get('user_email')
        # Check if HubSpot is connected for this user
        if request.user.is_authenticated:
            from .models import HubspotIntegration
            hubspot_connected = HubspotIntegration.objects.filter(user=request.user).exists()
    return render(request, 'advisor_agent/chat.html', {
        'is_logged_in': is_logged_in,
        'user_name': user_name,
        'user_email': user_email,
        'hubspot_connected': hubspot_connected,
    })




def google_auth_init(request):
    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": settings.GOOGLE_OAUTH2_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH2_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
                "scopes": settings.GOOGLE_OAUTH2_SCOPES
            }
        },
        scopes=settings.GOOGLE_OAUTH2_SCOPES
    )
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    request.session['oauth_state'] = state
    return redirect(authorization_url)

def google_auth_callback(request):
    state = request.session.get('oauth_state')
    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": settings.GOOGLE_OAUTH2_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH2_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
                "scopes": settings.GOOGLE_OAUTH2_SCOPES
            }
        },
        scopes=settings.GOOGLE_OAUTH2_SCOPES,
        state=state
    )
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    flow.fetch_token(authorization_response=request.build_absolute_uri())
    # Decode id_token for user info
    id_token = flow.credentials.id_token if hasattr(flow.credentials, 'id_token') else None
    user_name = None
    user_email = None
    id_token_decoded = None
    if id_token:
        id_token_decoded = jwt.decode(id_token, options={"verify_signature": False}, algorithms=["RS256"])
        print(id_token_decoded)
        user_email = id_token_decoded.get('email')
        user_name = id_token_decoded.get('name') or id_token_decoded.get('given_name')
    # Create or get Django user and log them in
    if user_email:
        User = get_user_model()
        user, created = User.objects.get_or_create(email=user_email, defaults={
            'username': user_email,
            'first_name': user_name or '',
        })
        login(request, user)
        # Gmail polling on sign-in
        from .models import GmailPollingState
        polling_state, _ = GmailPollingState.objects.get_or_create(user=user)
        creds_data = {
            'token': flow.credentials.token,
            'refresh_token': flow.credentials.refresh_token,
            'token_uri': flow.credentials.token_uri,
            'client_id': flow.credentials.client_id,
            "client_secret": settings.GOOGLE_OAUTH2_CLIENT_SECRET,
            'scopes': flow.credentials.scopes,
        }
        # Persist credentials for background polling
        polling_state.token = flow.credentials.token
        polling_state.refresh_token = flow.credentials.refresh_token
        polling_state.token_uri = flow.credentials.token_uri
        polling_state.client_id = flow.credentials.client_id
        polling_state.client_secret = settings.GOOGLE_OAUTH2_CLIENT_SECRET
        polling_state.scopes = ','.join(flow.credentials.scopes) if flow.credentials.scopes else ''
        polling_state.save()
        since_history_id = polling_state.last_history_id
        _, last_history_id = fetch_gmail_messages(creds_data, user.id, since_history_id=since_history_id)
        polling_state.last_history_id = last_history_id
        polling_state.save()
        # Fetch and store calendar events after login
        fetch_calendar_events(creds_data, user.id)
        # Register Google Calendar webhook for this user
        webhook_url = settings.BASE_URL + '/webhooks/google-calendar/'
        register_calendar_webhook(flow.credentials, webhook_url, user.id)
    # Store credentials and user info in session
    request.session['google_credentials'] = {
        'token': flow.credentials.token,
        'refresh_token': flow.credentials.refresh_token,
        'token_uri': flow.credentials.token_uri,
        'client_id': flow.credentials.client_id,
        "client_secret": settings.GOOGLE_OAUTH2_CLIENT_SECRET,
        'scopes': flow.credentials.scopes,
        'id_token': id_token,
        'id_token_decoded': id_token_decoded,
        'user_name': user_name,
        'user_email': user_email,
        'user_id': user.id,  # Ensure Django user id is available in session
    }
    request.session['user_name'] = user_name
    request.session['user_email'] = user_email
    return redirect('chat')

def ensure_refreshable_credentials(credentials):
    required = ['refresh_token', 'token_uri', 'client_id', 'client_secret']
    missing = [k for k in required if not credentials.get(k)]
    if missing:
        raise Exception(f"Missing fields for token refresh: {missing}. Please re-authenticate.")

def read_gmail(request):
    credentials = request.session.get('google_credentials')
    user_id = request.user.id
    try:
        ensure_refreshable_credentials(credentials)
        polling_state, _ = GmailPollingState.objects.get_or_create(user=request.user)
        since_history_id = polling_state.last_history_id
        messages, last_history_id = fetch_gmail_messages(credentials, user_id, since_history_id=since_history_id)
        # Update polling state
        polling_state.last_history_id = last_history_id
        polling_state.save()
        return JsonResponse({'messages': messages})
    except (RefreshError, Exception) as e:
        print(f"Google token refresh failed or credentials missing: {e}")
        request.session.pop('google_credentials', None)
        return redirect('google-auth')

def create_calendar_event(request):
    credentials = request.session.get('google_credentials')
    creds = Credentials(
        token=credentials['token'],
        refresh_token=credentials['refresh_token'],
        token_uri=credentials['token_uri'],
        client_id=credentials['client_id'],
        scopes=credentials['scopes']
    )
    # For now, use hardcoded values as before
    summary = 'Meeting Jump'
    start = '2025-12-25T10:00:00'
    end = '2025-12-25T11:00:00'
    event = create_google_calendar_event(creds, summary, start, end)
    return JsonResponse(event)

def read_calendar(request):
    creds_data = request.session.get('google_credentials')
    if not creds_data:
        return JsonResponse({'error': 'Not authenticated with Google'}, status=401)
    user_id = request.user.id
    events = fetch_calendar_events(creds_data, user_id)
    return JsonResponse({'events': events})

def logout_view(request):
    request.session.flush()
    return redirect('chat')

def get_full_message(service, user_id, msg_id):
    message = service.users().messages().get(userId=user_id, id=msg_id, format='full').execute()
    headers = message['payload'].get('headers', [])
    header_dict = {h['name']: h['value'] for h in headers}
    subject = header_dict.get('Subject')
    sender = header_dict.get('From')
    to = header_dict.get('To')
    date = header_dict.get('Date')
    # Extract body (plain text or html)
    body = ""
    if 'parts' in message['payload']:
        for part in message['payload']['parts']:
            if part['mimeType'] == 'text/plain':
                body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                break
    else:
        body = base64.urlsafe_b64decode(message['payload']['body']['data']).decode('utf-8')
    return {
        'id': msg_id,
        'subject': subject,
        'from': sender,
        'to': to,
        'date': date,
        'body': body,
    }


HUBSPOT_AUTH_URL = "https://app.hubspot.com/oauth/authorize"
HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"

@login_required
def hubspot_auth(request):
    params = {
        'client_id': settings.HUBSPOT_CLIENT_ID,
        'redirect_uri': settings.HUBSPOT_REDIRECT_URI,
        'scope': 'crm.objects.contacts.read crm.schemas.contacts.read crm.objects.companies.read crm.objects.contacts.write crm.objects.owners.read oauth',
    }
    url = f"{HUBSPOT_AUTH_URL}?{'&'.join([f'{k}={v}' for k,v in params.items()])}"
    return redirect(url)

# @login_required
def hubspot_callback(request):
    code = request.GET.get('code')
    if not code:
        return render(request, 'error.html', {'error': 'Authorization failed'})
    data = {
        'grant_type': 'authorization_code',
        'client_id': settings.HUBSPOT_CLIENT_ID,
        'client_secret': settings.HUBSPOT_CLIENT_SECRET,
        'redirect_uri': settings.HUBSPOT_REDIRECT_URI,
        'code': code
    }
    response = requests.post(HUBSPOT_TOKEN_URL, data=data)
    if response.status_code != 200:
        return render(request, 'error.html', {'error': 'Token exchange failed'})
    token_data = response.json()
    # Fetch HubSpot user/portal ID
    access_token = token_data['access_token']
    user_id = None
    portal_id = None
    userinfo_resp = requests.get(
        'https://api.hubapi.com/integrations/v1/me',
        headers={'Authorization': f'Bearer {access_token}'}
    )
    if userinfo_resp.status_code == 200:
        userinfo = userinfo_resp.json()
        user_id = userinfo.get('user_id')
        portal_id = userinfo.get('portalId')
    HubspotIntegration.objects.update_or_create(
        user=request.user,
        defaults={
            'access_token': token_data['access_token'],
            'refresh_token': token_data['refresh_token'],
            'expires_in': token_data['expires_in'],
            'hubspot_user_id': str(user_id or portal_id) if (user_id or portal_id) else None,
        }
    )
    # Automatically fetch and store contacts/notes after authentication
    fetch_hubspot_contacts_and_notes(request.user)
    return redirect('hubspot_contacts')

def refresh_tokens(user):
    try:
        integration = HubspotIntegration.objects.get(user=user)
        data = {
            'grant_type': 'refresh_token',
            'client_id': settings.HUBSPOT_CLIENT_ID,
            'client_secret': settings.HUBSPOT_CLIENT_SECRET,
            'refresh_token': integration.refresh_token
        }
        
        response = requests.post(HUBSPOT_TOKEN_URL, data=data)
        response.raise_for_status()
        
        token_data = response.json()
        integration.access_token = token_data['access_token']
        integration.refresh_token = token_data['refresh_token']
        integration.expires_in = token_data['expires_in']
        integration.token_created = timezone.now()
        integration.save()
        
        return token_data['access_token']
    except HubspotIntegration.DoesNotExist:
        return None
    except requests.exceptions.RequestException as e:
        print(f"Token refresh failed: {str(e)}")
        # logger.error(f"Token refresh failed: {str(e)}")
        return None

@login_required
def hubspot_contacts(request):
    contacts, contact_notes_data = fetch_hubspot_contacts_and_notes(request.user)
    return render(request, 'contacts.html', {
        'contacts': contacts,
        'contact_notes': contact_notes_data
    })


# hubspot_integration/views.py
@login_required
def create_contact(request):
    if request.method == 'POST':
        form = ContactForm(request.POST)
        if form.is_valid():
            try:
                create_hubspot_contact(
                    request.user,
                    form.cleaned_data['firstname'],
                    form.cleaned_data['lastname'],
                    form.cleaned_data['email'],
                    form.cleaned_data.get('phone', ''),
                    form.cleaned_data.get('company', ''),
                    form.cleaned_data.get('website', ''),
                )
                return redirect('hubspot_contacts')
            except Exception as e:
                error_msg = str(e)
                return render(request, 'create_contact.html', {'form': form, 'error': error_msg})
    else:
        form = ContactForm()
    return render(request, 'create_contact.html', {'form': form})

# hubspot_integration/views.py
@login_required
def create_note(request, contact_id):
    from .models import HubspotIntegration
    try:
        integration = HubspotIntegration.objects.get(user=request.user)
        access_token = get_valid_token(integration)
    except HubspotIntegration.DoesNotExist:
        return redirect('hubspot_auth')
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    contact_response = requests.get(
        f'https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}',
        headers=headers,
        params={'properties': 'firstname,lastname,email'}
    )
    if contact_response.status_code != 200:
        messages.error(request, 'Contact not found')
        return redirect('hubspot_contacts')
    contact = contact_response.json()
    if request.method == 'POST':
        form = NoteForm(request.POST)
        if form.is_valid():
            try:
                create_hubspot_note(
                    request.user,
                    contact_id,
                    form.cleaned_data['content'],
                )
                messages.success(request, 'Note added successfully!')
                return redirect('hubspot_contacts')
            except Exception as e:
                error_msg = str(e)
                messages.error(request, error_msg)
    else:
        form = NoteForm()
    contact_name = (
        f"{contact['properties'].get('firstname', '')} "
        f"{contact['properties'].get('lastname', '')}"
    ).strip() or contact['properties'].get('email', 'Contact')
    return render(request, 'create_note.html', {
        'form': form,
        'contact': contact,
        'contact_name': contact_name
    })


def get_valid_token(hubspot_integration):
        """Return a valid access token, refreshing if necessary"""
        token_age = timezone.now() - hubspot_integration.token_created
        if token_age.total_seconds() > hubspot_integration.expires_in - 300:  # Refresh 5 min before expiration
            return refresh_tokens(hubspot_integration.user)
        return hubspot_integration.access_token

@csrf_exempt
def google_calendar_webhook(request):
    if request.method == 'POST':
        channel_id = request.headers.get('X-Goog-Channel-ID')
        resource_state = request.headers.get('X-Goog-Resource-State')
        resource_id = request.headers.get('X-Goog-Resource-ID')
        user_token = request.headers.get('X-Goog-Channel-Token')
        # TODO: Map user_token or channel_id to user and credentials
        # For demo, assume user_token is user_id
        from django.contrib.auth import get_user_model
        User = get_user_model()
        try:
            user = User.objects.get(id=user_token)
        except Exception:
            return HttpResponse('User not found', status=404)
        polling_state = getattr(user, 'gmail_polling_state', None)
        if not polling_state:
            return HttpResponse('No credentials', status=404)
        creds_data = polling_state.get_google_credentials()
        # Fetch the latest event (could be improved to fetch only changed event)
        events = fetch_calendar_events(creds_data, user.id, max_results=1)
        # Retrieve ongoing instructions from vectorstore
        instructions = get_ongoing_instructions({'user_id': user.id})
        # Call agent with new event and instructions
        if events:
            event = events[0]
            agent_input = {
                'new_event': event,
                'ongoing_instructions': instructions,
            }
            agent_respond(user.id, json.dumps(agent_input), creds_data=creds_data)
        return HttpResponse('OK')
    else:
        return HttpResponse('Webhook endpoint for Google Calendar')

# Register webhook after Google OAuth login
def register_calendar_webhook(creds, webhook_url, user_token):
    from googleapiclient.discovery import build
    service = build('calendar', 'v3', credentials=creds)
    body = {
        "id": f"calendar-channel-{user_token}",
        "type": "web_hook",
        "address": webhook_url,
        "token": str(user_token),
    }
    service.events().watch(calendarId='primary', body=body).execute()

@csrf_exempt
def hubspot_webhook(request):
    if request.method == 'POST':
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except Exception:
            return HttpResponse('Invalid JSON', status=400)
        # HubSpot sends a list of events
        for event in payload.get('events', []):
            object_type = event.get('objectType')
            object_id = event.get('objectId')
            change_type = event.get('changeType')
            hubspot_user_id = str(event.get('userId'))  # Use as string for matching
            # Find the integration and user by hubspot_user_id
            from .models import HubspotIntegration
            try:
                integration = HubspotIntegration.objects.get(hubspot_user_id=hubspot_user_id)
                user = integration.user
            except HubspotIntegration.DoesNotExist:
                continue
            access_token = integration.access_token
            headers = {'Authorization': f'Bearer {access_token}'}
            # Fetch the full object from HubSpot
            if object_type == 'contact':
                url = f'https://api.hubapi.com/crm/v3/objects/contacts/{object_id}'
                resp = requests.get(url, headers=headers)
                if resp.status_code == 200:
                    contact = resp.json()
                    props = contact.get('properties', {})
                    name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
                    email = props.get('email', '')
                    phone = props.get('phone', '')
                    company = props.get('company', '')
                    summary = f"{name} <{email}> | Phone: {phone} | Company: {company}"
                    doc = {
                        'text': summary,
                        'external_id': object_id,
                        'name': name,
                        'email': email,
                        'phone': phone,
                        'company': company,
                        'type': 'contact',
                    }
                    from .vectorstore import add_documents_to_vectorstore
                    add_documents_to_vectorstore(user.id, [doc], source='hubspot_contact')
                    new_event = doc
            elif object_type == 'note':
                url = f'https://api.hubapi.com/crm/v3/objects/notes/{object_id}'
                resp = requests.get(url, headers=headers, params={'properties': 'hs_note_body,hs_timestamp,hubspot_owner_id'})
                if resp.status_code == 200:
                    note = resp.json()
                    props = note.get('properties', {})
                    content = props.get('hs_note_body', '')
                    created_at = props.get('hs_timestamp', '')
                    owner_id = props.get('hubspot_owner_id', '')
                    if '<' in content and '>' in content:
                        import html2text
                        text = html2text.html2text(content)
                    else:
                        text = content
                    doc = {
                        'text': text,
                        'external_id': object_id,
                        'created_at': created_at,
                        'owner_id': owner_id,
                        'type': 'contact_note',
                    }
                    from .vectorstore import add_documents_to_vectorstore
                    add_documents_to_vectorstore(user.id, [doc], source='hubspot_note')
                    new_event = doc
            else:
                continue
            # Retrieve ongoing instructions from vectorstore
            instructions = get_ongoing_instructions({'user_id': user.id})
            from .agent import agent_respond
            agent_input = {
                'new_event': new_event,
                'ongoing_instructions': instructions,
            }
            agent_respond(user.id, json.dumps(agent_input))
        return HttpResponse('OK')
    else:
        return HttpResponse('Webhook endpoint for HubSpot')