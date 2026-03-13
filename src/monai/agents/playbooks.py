"""Provisioning playbooks — domain-specific knowledge for the executor.

Instead of telling the LLM "register on Upwork" and hoping for the best,
playbooks provide step-by-step workflows with real URLs, expected selectors,
error recovery strategies, and verification flows.

This is what turns the executor from a random clicker into an effective agent.
"""

from __future__ import annotations

# ── Platform Registration Playbooks ──────────────────────────────────────

EMAIL_PLAYBOOKS = {
    "protonmail": {
        "name": "ProtonMail",
        "signup_url": "https://account.proton.me/signup",
        "steps": [
            "Navigate to https://account.proton.me/signup",
            "Select the FREE plan (click 'Get Proton for free' or similar)",
            "Fill in username field with the preferred username",
            "Set a strong password and confirm it",
            "Skip phone/email recovery if possible (click 'Skip' or 'Maybe later')",
            "If CAPTCHA appears, solve it",
            "Complete signup and verify the account is created",
            "Take a screenshot of the inbox as proof",
        ],
        "selectors": {
            "username": '#username, [name="username"], [id="username"]',
            "password": '#password, [name="password"], [id="password"]',
            "confirm_password": '#repeat-password, [name="passwordConfirm"]',
            "submit": 'button[type="submit"], .button-large',
        },
        "error_recovery": {
            "username taken": "Try adding random digits to the username (e.g. nexify2847)",
            "CAPTCHA": "Use the screenshot tool to see the CAPTCHA, then attempt to solve it",
            "rate limit": "Wait 60 seconds, then retry",
        },
    },
    "outlook": {
        "name": "Outlook/Hotmail",
        "signup_url": "https://signup.live.com/signup",
        "steps": [
            "Navigate to https://signup.live.com/signup",
            "Enter the desired email address (username@outlook.com)",
            "Click Next",
            "Set a strong password",
            "Enter first name and last name",
            "Select country and birthdate",
            "Solve CAPTCHA if presented",
            "Complete signup",
        ],
        "selectors": {
            "email": '#MemberName, [name="MemberName"]',
            "password": '#PasswordInput, [name="Password"]',
            "first_name": '#FirstName, [name="FirstName"]',
            "last_name": '#LastName, [name="LastName"]',
            "next_button": '#iSignupAction, button[type="submit"]',
        },
        "error_recovery": {
            "username taken": "Try a different username with numbers",
            "phone required": "Use the get_phone tool to acquire a virtual number, then enter the code",
            "CAPTCHA": "Attempt to solve the CAPTCHA challenge",
        },
    },
    "mail_tm": {
        "name": "mail.tm (Temp Email - API)",
        "note": "Use the create_temp_email tool instead of browser registration. "
                "This creates an instant disposable email via API — no browser needed.",
        "steps": [
            "Use the create_temp_email tool to get an instant email address",
            "The tool returns the email address and password",
            "Use this email for platform signups that need email verification",
            "Use check_email_verification tool to poll for verification codes",
        ],
    },
}

FREELANCE_PLAYBOOKS = {
    "upwork": {
        "name": "Upwork",
        "signup_url": "https://www.upwork.com/nx/signup/?dest=home",
        "steps": [
            "Navigate to https://www.upwork.com/nx/signup/?dest=home",
            "Click 'Sign Up' or look for the freelancer signup option",
            "Choose 'I want to work as a freelancer' if asked",
            "Fill in: first name, last name, email, password",
            "Click the signup/continue button",
            "If email verification is required, use check_email_verification tool",
            "After verification, complete the profile setup wizard",
            "Add a professional title and description",
            "Set an hourly rate",
            "Take screenshot as proof of completion",
        ],
        "selectors": {
            "first_name": '#first-name-input, [name="firstName"]',
            "last_name": '#last-name-input, [name="lastName"]',
            "email": '#redesigned-input-email, [name="email"]',
            "password": '#password-input, [name="password"]',
            "signup_button": 'button#button-submit-form, button[type="submit"]',
        },
        "error_recovery": {
            "email already registered": "Generate a new temp email with create_temp_email and retry",
            "phone verification": "Use get_phone tool to acquire a virtual number",
            "country not supported": "Use a US-based identity and proxy",
            "CAPTCHA": "Try solving; if it fails, wait 2 minutes and retry with a fresh browser session",
        },
        "notes": "Upwork often requires email verification before profile setup. "
                 "Always use a real-looking email, not obviously temp ones.",
    },
    "fiverr": {
        "name": "Fiverr",
        "signup_url": "https://www.fiverr.com/join",
        "steps": [
            "Navigate to https://www.fiverr.com/join",
            "Enter email address",
            "Choose a username and password",
            "Click 'Join'",
            "Complete email verification if required",
            "Set up seller profile: description, skills, portfolio",
            "Create at least one gig listing",
        ],
        "selectors": {
            "email": '[name="email"], #email',
            "username": '[name="username"], #username',
            "password": '[name="password"], #password',
            "join_button": 'button[type="submit"], .btn-join',
        },
        "error_recovery": {
            "email taken": "Use create_temp_email for a fresh address",
            "username taken": "Append random digits",
            "CAPTCHA": "Solve it; Fiverr uses hCaptcha",
        },
    },
    "freelancer": {
        "name": "Freelancer.com",
        "signup_url": "https://www.freelancer.com/signup",
        "steps": [
            "Navigate to https://www.freelancer.com/signup",
            "Select 'I want to work' / freelancer option",
            "Enter email, username, password",
            "Agree to terms",
            "Click signup button",
            "Complete email verification",
            "Fill in profile details",
        ],
        "selectors": {
            "email": '[name="email"], #email',
            "username": '[name="username"], #username',
            "password": '[name="password"], #password',
        },
    },
}

DOMAIN_PLAYBOOKS = {
    "namecheap": {
        "name": "Namecheap",
        "search_url": "https://www.namecheap.com/domains/registration/results/?domain=",
        "steps": [
            "Navigate to https://www.namecheap.com/domains/registration/results/?domain={domain}",
            "Check if the domain is available",
            "If available, click 'Add to cart'",
            "Click 'View Cart' or 'Checkout'",
            "If not logged in, create an account first",
            "Fill in payment details (use provided payment method)",
            "Complete purchase",
            "Take screenshot of confirmation",
        ],
        "error_recovery": {
            "domain taken": "Try alternative TLDs (.net, .io, .co) or add a prefix/suffix",
            "price too high": "Check .xyz or .online TLDs which are cheaper",
            "payment failed": "Verify payment method and retry",
        },
    },
    "porkbun": {
        "name": "Porkbun",
        "search_url": "https://porkbun.com/checkout/search?q=",
        "steps": [
            "Navigate to https://porkbun.com/checkout/search?q={domain}",
            "Check if the domain is available",
            "Click 'Add to cart' or the + button",
            "Proceed to checkout",
            "Create account if needed",
            "Complete purchase",
        ],
    },
}

SOCIAL_PLAYBOOKS = {
    "telegram_bot": {
        "name": "Telegram Bot",
        "note": "Telegram bots are created via the BotFather bot on Telegram, "
                "which requires a Telegram account first.",
        "steps": [
            "Navigate to https://web.telegram.org/ or use Telegram Web",
            "Log in with phone number (use get_phone if needed)",
            "Search for @BotFather",
            "Send /newbot command",
            "Provide a display name for the bot",
            "Provide a username (must end in 'bot')",
            "Copy the API token from BotFather's response",
            "Return the API token as the result",
        ],
    },
    "twitter": {
        "name": "Twitter/X",
        "signup_url": "https://x.com/i/flow/signup",
        "steps": [
            "Navigate to https://x.com/i/flow/signup",
            "Click 'Create account'",
            "Enter name, email (or phone), and date of birth",
            "Click Next and follow the flow",
            "Verify email or phone",
            "Choose a username",
            "Complete profile setup",
        ],
        "error_recovery": {
            "phone required": "Use get_phone tool",
            "email verification": "Use check_email_verification tool",
            "locked/suspended": "Account detected as bot — nothing we can do, move on",
        },
    },
}

# ── Playbook Lookup ──────────────────────────────────────────────────────

ALL_PLAYBOOKS = {
    **{f"email:{k}": v for k, v in EMAIL_PLAYBOOKS.items()},
    **{f"freelance:{k}": v for k, v in FREELANCE_PLAYBOOKS.items()},
    **{f"domain:{k}": v for k, v in DOMAIN_PLAYBOOKS.items()},
    **{f"social:{k}": v for k, v in SOCIAL_PLAYBOOKS.items()},
}


def get_playbook(platform: str) -> dict | None:
    """Look up a playbook by platform name (fuzzy match)."""
    platform_lower = platform.lower().strip()

    # Direct match in sub-dicts
    for source in (EMAIL_PLAYBOOKS, FREELANCE_PLAYBOOKS, DOMAIN_PLAYBOOKS, SOCIAL_PLAYBOOKS):
        if platform_lower in source:
            return source[platform_lower]

    # Fuzzy: check if platform name appears in any playbook name
    for key, playbook in ALL_PLAYBOOKS.items():
        name = playbook.get("name", "").lower()
        if platform_lower in name or platform_lower in key:
            return playbook

    return None


def get_playbook_prompt(platform: str) -> str:
    """Get a formatted prompt section for a platform's playbook."""
    playbook = get_playbook(platform)
    if not playbook:
        return ""

    lines = [f"\n## PLAYBOOK: {playbook.get('name', platform)}"]

    if playbook.get("note"):
        lines.append(f"NOTE: {playbook['note']}")

    if playbook.get("signup_url"):
        lines.append(f"URL: {playbook['signup_url']}")
    elif playbook.get("search_url"):
        lines.append(f"URL: {playbook['search_url']}")

    if playbook.get("steps"):
        lines.append("\nSTEPS:")
        for i, step in enumerate(playbook["steps"], 1):
            lines.append(f"  {i}. {step}")

    if playbook.get("selectors"):
        lines.append("\nKNOWN SELECTORS:")
        for field, sel in playbook["selectors"].items():
            lines.append(f"  {field}: {sel}")

    if playbook.get("error_recovery"):
        lines.append("\nERROR RECOVERY:")
        for error, fix in playbook["error_recovery"].items():
            lines.append(f"  If '{error}': {fix}")

    if playbook.get("notes"):
        lines.append(f"\nNOTES: {playbook['notes']}")

    return "\n".join(lines)


def detect_platforms_in_task(task: str) -> list[str]:
    """Detect which platforms are mentioned in a task description."""
    task_lower = task.lower()
    found = []

    # Check all known platforms
    all_platforms = set()
    for source in (EMAIL_PLAYBOOKS, FREELANCE_PLAYBOOKS, DOMAIN_PLAYBOOKS, SOCIAL_PLAYBOOKS):
        all_platforms.update(source.keys())

    for platform in all_platforms:
        if platform.replace("_", " ") in task_lower or platform in task_lower:
            found.append(platform)

    # Also check common aliases
    aliases = {
        "gmail": "mail_tm",  # redirect to temp email since Gmail requires phone
        "email": "mail_tm",
        "domain": "namecheap",
        "upwork": "upwork",
        "fiverr": "fiverr",
        "proton": "protonmail",
        "telegram": "telegram_bot",
    }
    for alias, platform in aliases.items():
        if alias in task_lower and platform not in found:
            found.append(platform)

    return found
