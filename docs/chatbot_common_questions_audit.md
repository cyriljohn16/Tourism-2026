# Chatbot Common Questions Coverage Audit

Date: 2026-05-06
Scope: chatbot QA coverage only. No Chapter 4 metrics, database schema, recommendation metric calculations, or thesis claims were changed.

## 1. Chatbot Coverage Summary

Overall status: Partial to Good.

Estimated coverage: about 75% to 80% for the supplied common-question set after the safe routing fixes in this audit. Coverage is strongest for guest accommodation discovery, accommodation preview/handoff, tour package listing and tour booking guidance, map/direction help, and role-specific staff navigation. Coverage is weaker for deeper free-form current-events questions, some informal multilingual phrases, and questions that depend on live external data.

Main strengths:

- The chatbot has deterministic pre-routing plus Text-CNN intent classification fallback for text intent classification.
- Accommodation recommendations use approved accommodation/room scope and Decision Tree recommendation helpers.
- Accommodation flow is mostly thesis-safe: search, room view, cost preview, and external handoff are supported; internal accommodation booking creation is avoided in chatbot flow.
- Tour booking flow is supported internally through tour schedule selection, pending request creation, staff review language, notifications, and email messaging.
- Maps/directions are supported through MapBookmark lookup, route/open-map actions, and approximate distance guidance when client location is available.
- Guest, owner, employee, and admin modes have separate help/navigation responses.
- Basic small talk, greetings, names, thanks, apologies, and identity questions are handled.

Main gaps:

- Some phrase variants still depend on generic fallback or broad accommodation discovery instead of exact targeted answers.
- Current/weather/political questions are correctly redirected after this audit, but no live data source exists.
- Cebuano/Bisaya support exists for selected phrases, but it is not complete conversational Cebuano support.
- Role-based help is mostly navigation/workflow guidance, not deep procedural training for every screen.
- Browser testing is still needed because card rendering, quick replies, map links, and external handoff buttons are front-end dependent.

## 2. Supported Intent Map

| Intent/category | Example user phrases | Current handling | Status | Relevant file/function |
|---|---|---|---|---|
| Greetings and small talk | hi, hello, good morning, thanks, sorry | Structured small-talk response with quick replies | Pass | ai_chatbot/views.py::_build_small_talk_payload |
| Identity/capability | who are you, what can you do | Explains Ibayaw Tour chatbot capabilities | Pass | ai_chatbot/views.py::_build_small_talk_payload, _build_role_help_payload |
| Guest help | help, menu | Guest assistant mode help with quick replies | Pass | ai_chatbot/views.py::_is_help_or_greeting_command, _build_role_help_payload |
| Bayawan overview | what is Bayawan known for | Safe verified-info wording and tourism options | Pass | ai_chatbot/views.py::_is_bayawan_encyclopedia_query |
| Tourist spots | tourist spots, what can I visit | Lists published/mapped tourism places when data exists | Pass | ai_chatbot/views.py::_build_bayawan_encyclopedia_payload, _is_map_contents_query |
| Dining | where can I eat, restaurants nearby | Lists mapped dining places when available | Pass | ai_chatbot/views.py::_is_dining_query, dining map branch |
| Trip planning | 10000 budget, 2 days, plan my trip | Budget stay planning flow with clarifying slots | Pass | ai_chatbot/views.py::_is_stay_planning_request, _build_budget_stay_plan_payload |
| Tour listing | show tours, tour packages | Lists available packages/schedules | Pass | ai_chatbot/views.py::_get_recommendations, _build_tour_schedule_listing_payload |
| Tour booking | book a tour, reserve a tour package | Creates pending tour request after confirmation and login | Pass | ai_chatbot/views.py::_submit_guest_tour_booking_request |
| Tour booking status | check my tour booking | Login-gated private action, links to tour bookings | Pass | ai_chatbot/views.py::_is_guest_view_tour_bookings_command |
| Tour payment guidance | where do I pay for the tour | Uses tour billing/payment guidance and official configured flow where available | Partial | ai_chatbot/views.py::_calculate_billing, tour booking response branches |
| Accommodation recommendation | recommend hotel, cheap inn, hotel in Suba | Decision Tree-backed recommendation with approved-room scope | Pass | ai_chatbot/views.py::_safe_get_accommodation_recommendations; ai_chatbot/recommenders.py |
| Room listing | show rooms, rooms for accommodation | Lists available rooms for selected/identified accommodation | Pass | ai_chatbot/views.py::_is_accommodation_room_listing_command |
| Accommodation preview | preview cost, estimate stay, how much for 2 nights | Creates cost preview only, no booking record | Pass | ai_chatbot/views.py::_build_accommodation_preview_response |
| Accommodation external handoff | open Facebook page, contact provider | Returns verified contact/provider link actions | Pass | ai_chatbot/views.py::_build_accommodation_link_actions |
| Accommodation unsafe booking/payment | hotel payment link, confirm hotel booking | Scope-safe clarification after audit | Pass | ai_chatbot/views.py::_is_accommodation_scope_safety_request |
| Maps | show map, open city map | Opens map page after audit | Pass | ai_chatbot/views.py::_is_guest_map_command |
| Directions | directions to Bayawan City Plaza | Looks up mapped place and returns map/direction action | Pass | ai_chatbot/views.py::_build_travel_guidance_payload, _lookup_mapbookmark_place |
| Origin travel guidance | from Manila/Cebu/Dumaguete to Bayawan | Gives practical route guidance without claiming live transport data | Partial | ai_chatbot/views.py::_build_travel_guidance_payload |
| Guest system usage | how do I use this system, need account | Guest FAQ response after audit | Pass | ai_chatbot/views.py::_is_guest_system_usage_query |
| Owner support | add rooms, update details, monthly reports | Owner role navigation/workflow help | Pass | ai_chatbot/views.py::_detect_owner_support_topic |
| Employee support | approve/decline tour assignment, records | Employee role guidance and assigned tour navigation | Pass | ai_chatbot/views.py::_detect_employee_support_topic |
| Admin support | monitor reports, approvals, records | Admin role navigation/workflow summaries | Pass | ai_chatbot/views.py::_detect_admin_support_topic |
| Fallback | unrelated, weather, medical/legal | Scope-aware redirect; safer real-time/professional-advice wording after audit | Pass | ai_chatbot/views.py::_build_out_of_scope_payload |
| Typo variants | accomodation, hotle, bok hotel | Expanded typo normalization after audit | Partial | ai_chatbot/views.py::_normalize_common_chat_typos |
| Cebuano/Bisaya basics | asa dapit, tag pila, naa moy hotel | Selected keyword support exists; not full Cebuano NLU | Partial | ai_chatbot/views.py::_multilingual_keyword_intent, typo/keyword routing |

## 3. Common Question Test Matrix

Legend: Pass means the current code has a direct or safe deterministic path. Partial means answerable but may require clarification, data availability, login, or browser UI. Fail means not specifically handled or likely fallback.

| Test prompt | Expected behavior | Actual/current behavior if known | Status | Notes |
|---|---|---|---|---|
| hi | Friendly greeting and options | Smoke test returned greeting and quick replies | Pass | Small talk |
| hello | Friendly greeting and options | Same small-talk path | Pass | Small talk |
| good morning | Greeting and help offer | Small-talk path | Pass | Small talk |
| good afternoon | Greeting and help offer | Small-talk path | Pass | Small talk |
| good evening | Greeting and help offer | Small-talk path | Pass | Small talk |
| my name is Renold | Remembers first name in session | Existing intro-name extraction | Pass | Session memory only |
| I am Renold | Remembers first name in session | Existing intro-name extraction | Pass | Session memory only |
| thank you | Polite acknowledgement | Existing thanks response | Pass | Small talk |
| thanks | Polite acknowledgement | Existing thanks response | Pass | Small talk |
| sorry | Reassuring response/help | Existing sorry branch | Pass | Small talk |
| who are you? | Explain chatbot identity | Added direct identity wording | Pass | Fixed |
| what can you do? | Explain capabilities | Help/small-talk path | Pass | Existing plus fixed identity |
| help | Show role help | Smoke test returned guest help | Pass | Role-aware |
| can you help me? | Capabilities and quick replies | Small-talk help branch | Pass | Existing |
| what can I visit in Bayawan? | Tourist spots or clarification | Tourism information/map path | Partial | Data-dependent |
| what tourist spots are available? | List published/mapped spots | Existing tourism info path | Pass | Data-dependent |
| recommend places to visit | Tour/tourism suggestions | Existing recommendation path | Partial | Can prefer tours |
| what can I do in Bayawan? | Clarify tours/accommodations/directions/planning | Existing clarification or tour path | Pass | Intent-safe |
| where can I eat? | Dining places | Smoke test returned dining places | Pass | Data-dependent |
| show dining places | Dining places/map cards | Existing map contents path | Pass | Data-dependent |
| are there restaurants nearby? | Dining map guidance | Dining query path | Pass | Data-dependent |
| what is Bayawan known for? | Safe Bayawan overview | Smoke test returned safe overview | Pass | Avoids overclaiming |
| give me a travel guide for Bayawan | Trip/travel guidance | Existing planning/guidance path | Partial | May ask destination/budget |
| help me plan my trip | Ask budget/days or plan | Existing stay planning path | Pass | Slot-based |
| I have 10000 budget, what can I do? | Budget plan | Existing stay planning path | Pass | Smoke tested earlier behavior exists |
| I will stay for 2 days, what do you suggest? | Ask budget or generate plan | Existing planning slot flow | Partial | Needs budget |
| recommend a hotel | Show approved stays or ask slots | Smoke test returned recommendations | Pass | Data-dependent |
| recommend an inn | Inn recommendations | Deterministic accommodation route | Pass | Data-dependent |
| cheap inn in Bayawan | Budget inn suggestions | Accommodation route | Pass | Data-dependent |
| hotel in Suba | Ask guests/budget or preview options | Accommodation route | Pass | Data-dependent |
| inn in Poblacion | Ask missing slots or recommend | Accommodation route | Pass | Data-dependent |
| accommodation for 2 guests | Ask location/budget or recommend | Accommodation route | Pass | Slot-based |
| hotel for 4 people | Ask location/budget or recommend | Accommodation route | Pass | Slot-based |
| room under 2000 | Ask type/location/guests | Accommodation route | Pass | Slot-based |
| affordable accommodation near the city | Budget accommodation | Accommodation route | Pass | Data-dependent |
| best hotel for family | Family preference accommodation | Accommodation preference extraction | Partial | Depends on tags/data |
| where can I stay? | Accommodation recommendations | Parity maps to accommodation | Pass | Existing |
| show available rooms | Ask accommodation or use selected context | Room listing path | Pass | May need target |
| show rooms for Hotel Maefinn | List rooms | Room listing path | Pass | Data-dependent |
| what rooms are available? | Ask accommodation or context | Room listing path | Partial | Needs target if no context |
| room good for 2 people | Accommodation slot flow | Accommodation route | Pass | Data-dependent |
| room with aircon | Amenity preference extraction | Accommodation route | Partial | Amenity data-dependent |
| how much is the room? | Preview/cost guidance | Accommodation billing/preview path | Partial | Needs room/stay details |
| can I book this hotel? | Clarify external booking/preview | Current broad discovery or preview guidance | Partial | Safer payment/direct-booking fixed |
| book room 144 | Preview flow, not internal accommodation booking | Booking preview path if room resolves | Partial | Data/context-dependent |
| I want to reserve a hotel | Preview/external handoff only | Accommodation preview route | Pass | Thesis-safe |
| I want to make a booking | Clarify tour vs accommodation | May route by context | Partial | Ambiguous |
| create booking preview | Ask room/stay/guest details | Preview flow | Pass | No DB booking |
| preview cost | Ask missing details | Preview flow | Pass | No DB booking |
| estimate my stay | Ask missing details | Preview flow | Pass | No DB booking |
| how much for 2 nights? | Ask room/guests if missing | Preview flow | Partial | Needs selected room |
| how much for 3 guests? | Ask room/nights if missing | Preview flow | Partial | Needs selected room |
| proceed to official page | Opens verified provider links if context exists | Handoff path | Partial | Needs context/link |
| open Facebook page | Opens verified Facebook if context exists | Handoff path | Partial | Needs selected accommodation |
| contact the accommodation | Opens contact/provider link if available | Handoff path | Partial | Needs context/link |
| where do I finalize the hotel booking? | Explain external finalization | Added how-to-book phrase coverage | Pass | Fixed |
| how do I complete my accommodation booking? | Explain external finalization | Added how-to-book phrase coverage | Pass | Fixed |
| show tours | List tours | Smoke test returned tours | Pass | Data-dependent |
| what tour packages are available? | List tours | Tour listing path | Pass | Data-dependent |
| book a tour | Start tour booking flow | Smoke test started booking details | Pass | Login needed to submit |
| I want to book a tour package | Start tour booking flow | Guest tour booking command | Pass | Login needed to submit |
| how do I reserve a tour? | Explain/select tour flow | System/tour booking path | Pass | Fixed FAQ helps |
| submit tour booking | Submit after selected schedule/details/confirmation | Tour pending creation path | Partial | Requires login/context |
| check my tour booking | Login-gated booking status | Smoke test required login | Pass | Private action |
| is my tour booking approved? | Link/status guidance | Tour booking status path | Partial | Needs login/record |
| what does pending mean? | Pending review explanation | Existing tour flow language | Partial | Could use more explicit FAQ later |
| how will I know if my tour booking is approved? | Email/status explanation | Existing tour booking language | Partial | Could add direct FAQ later |
| will I receive an email? | Tour email explanation | Tour submit path sends email | Partial | Direct FAQ could improve |
| where do I pay for the tour? | Tour payment guidance | Billing flow exists | Partial | Depends configured payment page |
| show map | Open city map | Smoke test now opens map | Pass | Fixed |
| where is Bayawan City Plaza? | Mapped place details/link | Smoke test directions path worked | Pass | Data-dependent |
| directions to Bayawan City Plaza | Directions/map action | Smoke test worked | Pass | Data-dependent |
| how far is Bayawan City Plaza? | Distance estimate if location available | Travel guidance path | Partial | Needs client location |
| how long does it take to go to Bayawan City Plaza? | ETA if location available | Travel guidance path | Partial | Needs client location |
| what places are on the map? | Map contents | Existing map contents path | Pass | Data-dependent |
| show accommodations on map | Mapped approved stays | Map category path | Pass | Data-dependent |
| show tourist spots on map | Mapped spots | Map category path | Pass | Data-dependent |
| show restaurants on map | Mapped restaurants | Map category path | Pass | Data-dependent |
| how do I go to Bayawan from Dumaguete? | Practical route guidance | Travel guidance path | Pass | No live transit data |
| how do I go to Bayawan from Cebu? | Practical route guidance | Travel guidance path | Pass | No live transit data |
| how do I go to Bayawan from Manila? | Practical route guidance | Travel guidance path | Pass | Smoke tested previously |
| how do I use this system? | Guest system help | Added FAQ response | Pass | Fixed |
| how do I search for hotels? | Hotel search instructions | Added/strengthened FAQ response | Pass | Fixed |
| how do I view rooms? | Room-view instructions | Added FAQ response | Pass | Fixed |
| how do I create a preview? | Preview instructions | Added FAQ response | Pass | Fixed |
| how do I book a tour? | Tour booking instructions | Added FAQ response | Pass | Fixed |
| how do I contact the Tourism Office? | Official-channel guidance | Added FAQ response | Pass | Fixed |
| where can I see my booking? | Login/private status guidance | Existing private action path | Partial | Ambiguous accommodation/tour |
| where can I see approved accommodations? | Show approved stays | Existing accommodation route | Pass | Data-dependent |
| how do I log in? | Login/account guidance | Added FAQ response | Pass | Fixed |
| do I need an account? | Account requirement explanation | Smoke test passed | Pass | Fixed |
| what can guests do? | Guest capability explanation | Added FAQ response | Pass | Fixed |
| can tourists use the chatbot? | Guest capability explanation | Added FAQ response | Pass | Fixed |
| can tourists book tours? | Tour booking explanation | Added FAQ response | Pass | Fixed |
| what can owners do? | Owner help | Role help in owner mode | Pass | Role-context dependent |
| how can owners add rooms? | Owner manage rooms guidance | Owner topic detection | Pass | Role-context dependent |
| how can owners update accommodation details? | Owner Hub guidance | Owner topic detection | Pass | Role-context dependent |
| how can owners submit monthly reports? | Owner reports guidance | Owner topic detection | Pass | Role-context dependent |
| what can employees do? | Employee help | Role help in employee mode | Pass | Role-context dependent |
| how can employees approve tour bookings? | Employee/admin workflow guidance | Employee/admin support paths | Partial | Actual approval likely staff page |
| how can employees decline tour bookings? | Employee/admin workflow guidance | Employee/admin support paths | Partial | Actual decline likely staff page |
| what can admins do? | Admin help | Role help in admin mode | Pass | Role-context dependent |
| can admins monitor reports? | Report/admin support | Admin reporting path | Pass | Role-context dependent |
| can admins override records? | Should avoid overclaiming | Generic admin guidance | Partial | Needs policy-specific wording |
| can you book my hotel directly? | Refuse internal booking; external handoff | Added scope safety/how-to coverage | Pass | Fixed |
| can I pay for my hotel here? | Refuse hotel payment link | Smoke test passed | Pass | Fixed |
| send me a hotel payment link | Refuse hotel payment link | Smoke test passed | Pass | Fixed |
| confirm my hotel booking | Refuse confirmation | Smoke test passed | Pass | Fixed |
| approve my accommodation booking | Refuse unsupported action | Scope safety path | Pass | Fixed |
| can you guarantee room availability? | Refuse guarantee | Smoke test passed | Pass | Fixed |
| can you answer math questions? | Redirect to tourism | Out-of-scope path | Pass | Existing |
| can you answer medical/legal questions? | Avoid professional advice | Safer fallback added | Pass | Fixed wording |
| what is the weather today? | No real-time data claim | Safer fallback added | Pass | Fixed wording |
| who is the president? | No current political info claim | Safer fallback added | Pass | Fixed wording |
| tell me something unrelated to Bayawan tourism | Redirect to tourism | Out-of-scope path | Pass | Existing |
| accomodation | Normalize to accommodation | Typo normalization exists | Pass | Existing |
| accomodations | Normalize to accommodations | Typo normalization exists | Pass | Existing |
| hotle | Normalize to hotel | Added typo normalization | Partial | May ask clarification if too vague |
| innn | Normalize to inn | Added typo normalization | Partial | May ask clarification if too vague |
| bok hotel | Normalize to book hotel | Smoke test routes to approved stays | Pass | Fixed |
| reserv room | Normalize reserve intent | Added typo normalization | Partial | Needs room context |
| tour pakage | Normalize tour package | Existing typo normalization | Pass | Existing |
| baywan | Normalize Bayawan | Added typo normalization | Pass | Fixed |
| bayawan tourist spoot | Normalize spot | Smoke test returned tourism places | Pass | Fixed |
| how to buk tour | Normalize book tour | Added typo normalization | Pass | Fixed |
| wer is hotel | Normalize where; route to accommodation | Added parity rule | Partial | Should avoid map miss after fix |
| direction to suba | Directions guidance | Travel guidance path | Pass | Data-dependent |
| cheap room pls | Budget room slot flow | Smoke test asked area | Pass | Fixed pls normalization |
| naa moy hotel? | Basic Cebuano accommodation intent | Smoke test returned approved stays | Partial | Limited Cebuano support |
| asa dapit ang hotel? | Basic Cebuano/map/accommodation help | Existing multilingual/keyword path | Partial | Can still be imprecise |
| tag pila ang room? | Price/accommodation slot flow | Smoke test asked guests/budget | Partial | Cebuano price intent handled loosely |

## 4. Thesis Scope Compliance Check

| Scope item | Finding | Status |
|---|---|---|
| Accommodation booking is not internally processed by chatbot | Chatbot preview paths call `_build_accommodation_preview_response` and store session state only. No `AccommodationBooking.objects.create` was found in chatbot accommodation flow. | Pass |
| Accommodation email/payment links are not generated by chatbot | Preview text says cost preview only and handoff uses verified provider/contact links. New safety handler refuses hotel payment links and confirmation. | Pass |
| Accommodation external handoff is allowed | Handoff functions sanitize third-party URLs and expose Facebook/verified provider contact links. | Pass |
| Tour booking is internally processed | `_submit_guest_tour_booking_request` creates `Pending` tour request, updates schedule slots, notifies staff/guest, and sends tour email. | Pass |
| CNN use | Text-CNN is used for text intent classification. There is also image-CNN helper code for image-assisted tourism category prediction, but the thesis statement here says CNN is text-based intent classification only. Do not expand or cite image-CNN as thesis result unless already approved. | Partial / watch item |
| Decision Tree use | Accommodation recommendation helpers are in `ai_chatbot/recommenders.py`, with decision-tree runtime status exposed. | Pass |
| Gemini use | Gemini/OpenAI NLG wrapper rewrites backend replies when configured; backend structured templates remain source of truth. | Pass |
| No misleading chatbot claims | After fixes, real-time/weather/professional advice and hotel payment/confirmation prompts are redirected safely. | Pass |

Important note: `guest_app/views.py` still contains legacy/internal accommodation booking views and models in the wider project. This audit did not remove them because the requested scope was chatbot QA and safe chatbot improvement. For thesis alignment, browser navigation should make clear that accommodation booking in chatbot is preview/external handoff only.

## 5. Fallback Analysis

Prompts that previously or still may trigger fallback too often:

- Single-word vague prompts: `hotel`, `inn`, `hotle`, `accommodation` often need clarification because they lack guests/location/budget.
- Contextual handoff prompts without selected room/accommodation: `open Facebook page`, `contact accommodation`, `proceed to official page` need prior context.
- Deep role questions outside role session: `how can owners add rooms?` in guest mode may not behave like owner mode.
- Free-form Cebuano/Bisaya beyond known keywords may route to multilingual fallback or generic clarification.
- `what does pending mean?`, `will I receive an email?`, and `where do I pay for the tour?` are answerable through flow context but could benefit from direct FAQ handlers.

Fallback quality:

- Guest fallback is warm and scope-aware.
- Staff fallbacks are role-aware and suggest dashboard/help actions.
- After this audit, current/weather/political prompts avoid hallucinating real-time data.
- After this audit, medical/legal/financial prompts avoid professional advice.

## 6. Recommended Fixes

| File | Function/section | Exact reason | Risk | Affects thesis results? |
|---|---|---|---|---|
| ai_chatbot/views.py | `_normalize_common_chat_typos` | Add common typos: hotle, innn, bok, buk, reserv, baywan, spoot, wer, pls | Low | No |
| ai_chatbot/views.py | `_build_small_talk_payload` | Answer `who are you?` directly instead of generic fallback | Low | No |
| ai_chatbot/views.py | `_is_accommodation_how_to_book_request` | Catch finalize/complete hotel booking variants safely | Low | No |
| ai_chatbot/views.py | new accommodation scope-safety handler | Refuse hotel payment links, internal confirmations, direct hotel booking, guaranteed availability | Low | No |
| ai_chatbot/views.py | map routing guard | Prevent `show map` from being treated as unknown place lookup | Low | No |
| ai_chatbot/views.py | guest system usage FAQ handler | Cover account/login/search/preview/tour booking common questions | Low | No |
| ai_chatbot/views.py | `_build_out_of_scope_payload` | Add safer wording for weather/current/political and medical/legal/financial prompts | Low | No |
| ai_chatbot/views.py | tour FAQ direct handlers | Add explicit `pending`, `email`, `tour payment` FAQ responses | Low | No, recommended future small patch |
| ai_chatbot/views.py | role FAQ cross-role support | If guest asks `what can owners do?`, explain role generally without needing owner session | Low | No, recommended future small patch |
| ai_chatbot/tests.py or docs | common prompt regression checklist | Preserve QA coverage | Low | No |
| guest/admin templates | Browser UI labels for accommodation links | Ensure labels say preview/contact, not confirmed booking/payment | Medium | No, but visual regression test needed |
| legacy guest accommodation booking views | Wider thesis-scope review | Wider project still has internal accommodation booking pages/models | High | Possibly, discuss before changing |

## 7. Safe Fixes Implemented

Implemented in `ai_chatbot/views.py`:

- Expanded typo normalization for common English and informal prompt variants.
- Added direct identity response for `who are you?`.
- Added safe accommodation scope response for hotel payment links, direct hotel booking, internal confirmation/approval, and room availability guarantees.
- Expanded accommodation completion/finalization phrases to explain external handoff.
- Added guest system usage FAQ responses for account, login, hotel search, room viewing, preview creation, tour booking, Tourism Office contact, and guest capabilities.
- Fixed `show map` routing so it opens the map instead of trying to look up a place named `show map`.
- Added safer out-of-scope wording for real-time/current/weather/political and medical/legal/financial prompts.

## 8. Test File Or Document

This file is the test/audit document: `docs/chatbot_common_questions_audit.md`.

Recommended manual browser smoke prompts after these changes:

- `show map`
- `who are you?`
- `can I pay for my hotel here?`
- `send me a hotel payment link`
- `confirm my hotel booking`
- `where do I finalize the hotel booking?`
- `how do I use this system?`
- `how do I search for hotels?`
- `do I need an account?`
- `hotle`
- `bok hotel`
- `baywan tourist spoot`
- `wer is hotel`
- `naa moy hotel?`
- `tag pila ang room?`

## 9. Files Inspected

Primary files inspected:

- `ai_chatbot/views.py`
- `ai_chatbot/urls.py`
- `ai_chatbot/models.py`
- `ai_chatbot/recommenders.py`
- `ai_chatbot/chat_services/response_templates.py`
- `ai_chatbot/chat_services/role_intent_registry.py`
- `guest_app/views.py`
- `guest_app/templates/components/guest_chat_widget.html`
- `admin_app/templates/components/chat_widget.html`
- `static/css/guest_unified.css`
- `artifacts/text_cnn_intent*/label_map.json` and related artifact paths by file discovery
- `ai_chatbot/tests.py` by search/reference

Verification performed:

- `python -m py_compile ai_chatbot/views.py`
- Focused Django Client smoke tests through `/api/chat/` with `HTTP_HOST=localhost`.

Residual manual testing needed:

- Browser rendering of recommendation cards, link actions, map buttons, and quick replies.
- Logged-in guest tour booking submission from selection through pending status.
- Owner, employee, and admin chatbot widgets in their actual dashboards.
- Accommodation preview with a real selected room and official/Facebook handoff button.
- Mobile chat widget behavior after quick reply clicks.


## 10. Follow-up Low-Risk FAQ Improvements (2026-05-06)

Additional safe chatbot improvements were added after the initial audit. These changes are routing/response-only and do not change schema, Chapter 4 metrics, evaluation outputs, recommendation computation, or accommodation transaction behavior.

New direct tour FAQ prompts covered:

| Prompt | Expected safe response | Status |
|---|---|---|
| what does pending mean? | Explain that pending tour booking requests are waiting for Tourism Office staff review before payment. | Pass |
| will I receive an email? | Explain that tour booking submissions/status updates may be emailed to the registered address and can be checked in My Tour Bookings. | Pass |
| where do I pay for the tour? | Explain payment happens only after review/approval through official configured handoff; no accommodation payment behavior added. | Pass |
| check my tour booking | If not logged in, ask user to log in; if logged in, route to existing My Tour Bookings payload. | Pass |

New basic Cebuano/Bisaya phrase prompts covered:

| Prompt | Expected safe response | Status |
|---|---|---|
| naa moy hotel? | Route to existing approved accommodation discovery/listing response. | Pass |
| tag pila ang room? | Explain room prices depend on room/stay details and offer approved stays/rooms/preview; no internal accommodation booking/payment. | Pass |
| asa dapit ang hotel? | Route to approved accommodations plus City Map/directions guidance. | Pass |
| naa moy tour package? | Route to existing tour schedule/listing response. | Pass |
| pila ang tour? | Explain tour prices are shown on configured tour packages/schedules and route to existing tour listing. | Pass |

Remaining low-risk gaps after this follow-up:

- Cebuano/Bisaya support is still phrase-based, not full conversational translation.
- Tour FAQ is now clearer for common questions, but the full booking submission still needs browser/manual testing with a logged-in guest and actual available schedules.
- Accommodation preview still depends on selecting a real room and dates before the official contact handoff can be fully tested.

Additional verification performed:

- `python -m py_compile ai_chatbot/views.py`
- Focused smoke tests through `/api/chat/` for the nine prompts above.
