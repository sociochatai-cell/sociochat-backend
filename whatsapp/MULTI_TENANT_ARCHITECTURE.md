# WhatsApp Multi-Tenant Architecture

## Overview

The Sociovia WhatsApp integration supports multiple users/businesses with complete data isolation.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Multi-Tenant Flow                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  User A (workspace_id='8')          User B (workspace_id='99')     │
│  ┌─────────────────────┐            ┌─────────────────────┐        │
│  │ WhatsApp Account 13 │            │ WhatsApp Account 14 │        │
│  │ Phone: +919667796730│            │ Phone: +1234567890  │        │
│  │ Token: ****ABC      │            │ Token: ****XYZ      │        │
│  └─────────────────────┘            └─────────────────────┘        │
│          │                                  │                       │
│          ▼                                  ▼                       │
│  ┌─────────────────────┐            ┌─────────────────────┐        │
│  │ Flows: 4, 6, 7, 14  │            │ Flows: 15, 16       │        │
│  │ Templates: 10-20    │            │ Templates: 21-25    │        │
│  └─────────────────────┘            └─────────────────────┘        │
│                                                                     │
│  User A CANNOT see User B's data (and vice versa)                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Database Schema

```
whatsapp_accounts
├── id (PK)
├── workspace_id  ← Links to user's workspace (ISOLATION KEY)
├── waba_id
├── phone_number_id
├── access_token_encrypted  ← Each user's own token
└── is_active

whatsapp_flows
├── id (PK)
├── account_id (FK → whatsapp_accounts.id)  ← Links to account
└── ...

whatsapp_templates
├── id (PK)
├── account_id (FK → whatsapp_accounts.id)  ← Links to account
└── ...
```

## New User Flow

### Step 1: User Logs In
- User is assigned a `workspace_id` (e.g., '99')
- Frontend stores: `localStorage.setItem('sv_whatsapp_workspace_id', '99')`

### Step 2: User Connects WhatsApp
```
Frontend → GET /api/whatsapp/connect/popup?workspace_id=99
         → Meta OAuth Flow
         → GET /api/whatsapp/connect/callback?code=...&state=99
Backend  → Creates WhatsAppAccount with workspace_id='99'
         → Auto-registers phone number
```

### Step 3: User Creates Flows/Templates
```
Frontend → POST /api/whatsapp/flows (account_id from user's account list)
Backend  → Flow created with account_id pointing to user's account
```

### Step 4: API Isolation
```python
# When user fetches accounts:
GET /api/whatsapp/accounts?workspace_id=99
→ Returns ONLY accounts where workspace_id='99'

# Token helper only finds accounts in same workspace:
get_account_with_token(account_id)
→ Falls back only to accounts with same workspace_id
```

## Security: Token Helper Isolation

```python
# In token_helper.py - Fallback ONLY within same workspace
def get_account_with_token(account_id):
    account = WhatsAppAccount.query.get(account_id)
    
    if not account.is_active or not account.get_access_token():
        # IMPORTANT: Only search within SAME workspace
        alt_account = WhatsAppAccount.query.filter_by(
            workspace_id=account.workspace_id,  # ← ISOLATION
            is_active=True
        ).first()
```

## Why User A Can't Access User B's Token

1. **Frontend**: Only shows accounts for user's workspace_id
2. **Backend**: `GET /accounts?workspace_id=X` filters by workspace
3. **Token Helper**: Fallback search is limited to same workspace_id
4. **OAuth**: New accounts are created with the connecting user's workspace_id

## Unverified Account Handling

If a user's WhatsApp account is not yet verified by Meta:

1. **Account is stored** but may have `is_active=False`
2. **Token is saved** (encrypted)
3. **User sees status** in the UI
4. **Once verified** by Meta, the account becomes usable
5. **No cross-user access** - unverified accounts can't borrow tokens from other users
