# `/breaking` — golden benchmark (good-to-follow reference)

This is the **canonical example + standards** for `manzill.com/breaking`. It is the "good version" to
match, so the requirements for **title, description, timeline, key facts, and sources** don't have to
be re-explained. The generator (`scripts/build_breaking_news.py`) should reproduce this shape every
run; reviewers should diff the live page against it.

`/breaking` is a **living corruption/accountability tracker for Rajasthan**: one title on the *current*
case, a timeline that **clubs the month's corruption/accountability cases**, and varied named sources.
It runs continuously — developments accumulate over the month; it does **not** wait on any government
update, and never softens into a pro-government or neutral piece.

---

## Golden example (based on the 19 Jul 2026 output, polished to standard)

### शीर्षक (title)
> राजस्थान में भ्रष्टाचार के आरोपों पर एसीबी की जाँच, सात अधिकारियों को निलंबित, नागरिकों को राहत नहीं

*Current aggregate ("सात अधिकारियों को निलंबित") + the accountability/citizen angle
("नागरिकों को राहत नहीं"). Hard news, Devanagari only, never neutral or praising.*

### पूरी खबर (description — hard news, inverted pyramid, attributed)
राजस्थान में भ्रष्टाचार पर कार्रवाई का सिलसिला जारी है — 19 जुलाई को ग्रेटर जयपुर कॉरपोरेशन (जीसीसी) ने
सात अधिकारियों को निलंबित किया, जो इस महीने एंटी-करप्शन ब्यूरो (एसीबी) की ताबड़तोड़ कार्रवाइयों की कड़ी
में नवीनतम है। नागरिक समूहों और विपक्ष के अनुसार, निलंबनों के बावजूद प्रभावित नागरिकों को अब तक कोई
स्पष्ट राहत या मुआवज़ा नहीं मिला।

इस महीने की शुरुआत में — 2 और 3 जुलाई को — एसीबी ने सरकारी व कृषि विभाग के अधिकारियों से अलग-अलग दो लाख
तिरसठ हजार रुपये की रिश्वत बरामद की; 6 जुलाई को कृषि विभाग ने तीन अधिकारियों को निलंबित किया। 10 जुलाई को
एसीबी ने दो और अधिकारियों को रिश्वत के मामले में पकड़ा, जिससे स्पष्ट हुआ कि भ्रष्टाचार कई विभागों में
फैला हुआ है।

17-18 जुलाई को राज्य सरकार ने आरजीएचएस के 51 अस्पतालों को अनियमितताओं के कारण निलंबित किया, जिससे ग्रामीण
स्वास्थ्य सेवाओं पर असर पड़ा; प्रभावित कर्मचारियों और रोगियों ने पुनर्वास व मुआवज़े की माँग की। विपक्ष और
नागरिक समूहों ने माँग की कि निलंबित अधिकारियों के खिलाफ शीघ्र चार्जशीट हो और प्रभावितों को उचित मुआवज़ा
मिले — इन पर अब तक कोई स्पष्ट योजना सामने नहीं आई।

*Note: the raw 19 Jul output ended with the outlet's own "सरकार … को यह स्पष्ट करना चाहिए" — that is
an **editorial** line and is **not** the standard. The polished version above attributes the demand to
विपक्ष/नागरिक समूह instead.*

### घटनाक्रम — शुरुआत से अब तक (timeline — clubs the month's cases, oldest → newest)
1. **2 जुलाई, रात 9:22 बजे** — एसीबी ने दो सरकारी अधिकारियों से दो लाख तिरसठ हजार रुपये बरामद किए,
   जिससे भ्रष्टाचार के संकेत मिले। *(द न्यू इंडियन एक्सप्रेस)*
2. **3 जुलाई, दोपहर 12:30 बजे** — जयपुर में कृषि विभाग के दो अधिकारियों से समान राशि बरामद की गई,
   जिससे विभागीय भ्रष्टाचार का पता चला। *(उदयपुर टाइम्स)*
3. **6 जुलाई, दोपहर 12:30 बजे** — राजस्थान कृषि विभाग ने तीन अधिकारियों को निलंबित किया, परंतु निलंबन के
   बाद किसानों को कोई स्पष्ट राहत नहीं मिली।
4. **10 जुलाई, रात 11:57 बजे** — एसीबी ने दो और अधिकारियों को रिश्वत के मामले में पकड़ लिया, जिससे
   भ्रष्टाचार की जाँच का दायरा बढ़ा। *(पंजाब केसरी)*
5. **17 जुलाई, रात 11:47 बजे** — राजस्थान सरकार ने आरजीएचएस के 51 अस्पतालों को अनियमितताओं के कारण
   निलंबित किया, जिससे ग्रामीण स्वास्थ्य सेवाओं में बाधा आई। *(टाइम्स ऑफ इंडिया)*
6. **18 जुलाई, दोपहर 1:47 बजे** — निलंबित अस्पतालों के कर्मचारियों और रोगियों ने उचित पुनर्वास व मुआवज़े
   की माँग की, परंतु कोई स्पष्ट योजना नहीं बताई गई।
7. **19 जुलाई, सुबह 7:58 बजे** — ग्रेटर जयपुर कॉरपोरेशन ने सात और अधिकारियों को निलंबित किया, लेकिन
   निलंबन के पीछे की प्रक्रिया और प्रभावित नागरिकों के अधिकारों पर सवाल बने रहे।

*7 dated steps, each a 2-3 sentence sourced account with a real date label + outlet. The steps are
different corruption cases across the month, **clubbed** into one arc.*

### मुख्य तथ्य (key facts — clean dated bullets)
- 2 जुलाई, रात 9:22 बजे: एसीबी ने दो सरकारी अधिकारियों से 2.63 लाख रुपये बरामद किए
- 3 जुलाई, दोपहर 12:30 बजे: एसीबी ने कृषि विभाग के दो अधिकारियों से 2.63 लाख रुपये बरामद किए
- 6 जुलाई, दोपहर 12:30 बजे: कृषि विभाग ने तीन अधिकारियों को निलंबित किया
- 10 जुलाई, रात 11:57 बजे: एसीबी ने दो अधिकारियों को रिश्वत मामले में पकड़ा
- 17-18 जुलाई: राजस्थान सरकार ने 51 आरजीएचएस अस्पतालों को अनियमितताओं के कारण निलंबित किया
- 19 जुलाई, सुबह 7:58 बजे: ग्रेटर जयपुर कॉरपोरेशन ने सात अधिकारियों को निलंबित किया

### पुलिस की जवाबदेही
एसीबी की कार्रवाई के बावजूद, पुलिस ने प्रारंभिक रिपोर्टिंग और त्वरित जाँच में देरी की, जिससे कई मामलों में
भ्रष्टाचार के प्रमाण एकत्र करने में बाधा आई। निलंबित अधिकारियों के खिलाफ तुरंत चार्जशीट न बनना और प्रभावित
नागरिकों को मुआवज़ा न मिलना, नागरिक समूहों के अनुसार, प्रशासनिक लापरवाही के संकेत हैं।

### आगे क्या
जाँच के आधार पर संबंधित अधिकारियों के खिलाफ चार्जशीट अपेक्षित है; विपक्ष और नागरिक समूहों ने प्रभावितों को
मुआवज़ा व पुनर्वास तथा दोषियों पर शीघ्र कार्रवाई की माँग की है।

### स्रोत (sources — varied named outlets, each with a real Hindi title)
| आउटलेट | शीर्षक |
|--------|--------|
| द न्यू इंडियन एक्सप्रेस | एसीबी ने दो अधिकारियों से दो लाख तिरसठ हजार रुपये बरामद किए |
| उदयपुर टाइम्स | एसीबी ने कृषि विभाग के दो अधिकारियों से दो लाख तिरसठ हजार रुपये बरामद किए |
| टाइम्स ऑफ इंडिया | राजस्थान कृषि विभाग ने तीन अधिकारियों को निलंबित किया |
| पंजाब केसरी | एसीबी ने दो अधिकारियों को रिश्वत मामले में पकड़ा |
| ज़ूम न्यूज़ | राजस्थान सरकार ने 51 आरजीएचएस अस्पतालों को अनियमितताओं के कारण निलंबित किया |

*Varied, named outlets — **never** "ताज़ा रिपोर्ट" on every card. Each card carries a real one-line
Hindi title tied to a timeline case.*

### यह भी ब्रेकिंग
Only **accountability** stories that question the government/police. A pro-government item — e.g. "जयपुर
विकास प्राधिकरण ने अवैध इमारतों को ध्वस्त किया … सुधार की उम्मीद" — is **not allowed** here.

---

## Standards checklist (the rules, per section)

- **Title** — hard news on the *current* case + the month's aggregate; foregrounds accountability +
  citizen impact (मुआवज़ा / राहत / जवाबदेही); never neutral or praising; Devanagari only.
- **Description (पूरी खबर)** — hard news, **inverted pyramid** (newest development first), **attributed**
  (विपक्ष/नागरिकों/एसीबी के अनुसार); **clubs the month's related corruption cases**; **no** editorial
  "सरकार को … करना चाहिए", **no** government praise.
- **Timeline (घटनाक्रम)** — ≥5 dated steps, **clubs the month's corruption/accountability cases**
  oldest → newest, real date label + reporting outlet, 2-3 sentence sourced text. **Never** raw data
  dumps (`[{'_': …}]`, joined arrays) or stray `:` / `–`.
- **Key facts (मुख्य तथ्य)** — clean dated bullets (who, department, amount, action).
- **Sources (स्रोत)** — **varied, named outlets** each with a **real Hindi title**; never the pale
  "ताज़ा रिपोर्ट" on every card.
- **यह भी ब्रेकिंग** — accountability-only (`has_failure_angle`); no pro-government cards.
- **Global** — fully **Devanagari** (`to_hindi`; acronyms → जेडीए/भाजपा/ईडी…); **no fabrication**
  (only sourced facts + attributed questions; no invented amounts/allegations about named people); no
  field-name/bracket tags (`(analysis)`, `(lead_story)`); the request stays within the **Groq TPM
  budget** (check with `python scripts/check_tpm.py`).

## How this maps to the code
- Clubbed timeline → `month_accountability_arc()` (aggregates the month's on-beat archive points).
- Varied titled sources → `arc_sources()` + the AI's `sources_hi`, with `HINDI_SOURCE` covering the
  common outlets.
- Devanagari + no raw dumps → `to_hindi` / `_ai_str` / `_ai_str_list` in `_lead_from_ai`.
- Accountability gating → `has_failure_angle` / `questions_authority` (`apply_policy_lead`,
  `order_secondary`).
- Hard-news, attributed, clubbed voice → the `_groq_messages` prompt.
