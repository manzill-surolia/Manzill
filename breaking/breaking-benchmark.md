# `/breaking` — golden benchmark (good-to-follow reference)

This is the **canonical set of worked examples + standards** for `manzill.com/breaking`, so the
requirements for **title, description, timeline, key facts, and sources** don't have to be re-explained.
The generator (`build_breaking_news.py`) should reproduce these shapes every run; reviewers
should diff the live page against them.

`/breaking` is a **living single-story breaking-news tracker for Jaipur** (Jaipur-only): the city's top
breaking and political news, one title on the *current* story, a hard-news attributed voice, and varied
named sources. It runs continuously — developments accumulate over the month.

There are **two use cases** — the timeline section has **two modes** — and this file documents both.
The two modes are **beat-agnostic**: they describe how the timeline is shaped (one developing story's
chronology vs the month's different stories), not the topic. The worked examples below happen to use an
accountability story, but the same shapes apply to any Jaipur story (crime, civic, political, court). A
machine-readable copy of both (for the AI to learn/adapt from) lives in **`breaking-cases.json`**.

---

## Which mode, when (the decision the page makes)

| | **Use Case A — `घटनाक्रम`** | **Use Case B — `इस महीने`** |
|---|---|---|
| **What the lead is** | ONE developing story with a chronology of its own (an event unfolding over days; a sustained situation) | A one-off lead (a single incident) with no chronology of its own |
| **What the list shows** | that single story's arc: पृष्ठभूमि → घटना → कार्रवाई → आधिकारिक प्रतिक्रिया → आगे | the month's **different** Jaipur stories, one line per **distinct** story (not a false chronology) |
| **Heading / note** | `घटनाक्रम` / "इसी खबर का सिलसिला — नवीनतम अपडेट सबसे ऊपर।" | `इस महीने` / "इस महीने की अलग-अलग खबरें — नवीनतम सबसे ऊपर।" |
| **Count word** | `घटनाक्रम` | `खबरें` |
| **Both always** | **≥5 steps · descending (newest first) · scroll-triggered reveal · pulse on the top item · real date label + outlet · 2–3 sentence sourced text** | same |

**Code:** `build()` picks the mode from the lead's own dated-point count — `len(own_pts) >=
SINGLE_CASE_MIN` (=3) → **case**; else, if the month has clubbable archived points → **month**; else
**case** with whatever own points exist. The heading/note flow through
`state` into the template placeholders `{{TIMELINE_HEADING}}` / `{{TIMELINE_NOTE}}`.

> **Never** confuse the two: a digest of unrelated stories under `घटनाक्रम — शुरुआत से अब तक` is a
> category error (unrelated stories are not a chronology). That is exactly the bug the two-mode split
> fixed.

---

## Use Case A — `घटनाक्रम` (one developing case)

*Based on the 19 Jul 2026 output (a sustained ACB anti-corruption drive tracked start-to-now as one
story). Presented polished to standard; see "capture deltas" below for where the raw screenshot
deviated.*

### शीर्षक (title)
> राजस्थान में भ्रष्टाचार के आरोपों पर एसीबी की जाँच, सात अधिकारियों को निलंबित, नागरिकों को राहत नहीं

*Current aggregate ("सात अधिकारियों को निलंबित") + the accountability/citizen angle ("नागरिकों को राहत
नहीं"). Hard news, Devanagari only, never neutral or praising.*

### पूरी खबर (description — hard news, inverted pyramid, attributed)
राजस्थान में हालिया भ्रष्टाचार विरोधी अभियानों में एंटी-कोरप्शन ब्यूरो (एसीबी) ने कई सरकारी विभागों के
अधिकारियों से रिश्वत लेन-देन के आरोपों को उजागर किया है। दो जुलाई को एसीबी ने दो सरकारी अधिकारियों से दो लाख
तिरसठ हजार रुपये बरामद किए, जबकि अगले दिन कृषि विभाग के दो अधिकारियों से समान राशि की बरामदगी की गई।

10 जुलाई को एसीबी ने दो और अधिकारियों को रिश्वत के मामले में पकड़ लिया; 17-18 जुलाई को राज्य सरकार ने
आरजीएचएस के 51 अस्पतालों को अनियमितताओं के कारण निलंबित कर दिया, और 19 जुलाई को ग्रेटर जयपुर कॉरपोरेशन ने
सात और अधिकारियों को निलंबित किया — परंतु निलंबन की प्रक्रिया और प्रभावित नागरिकों के अधिकारों पर सवाल बने
हुए हैं।

नागरिक समूहों और विपक्ष के अनुसार, इन निलंबनों के बावजूद प्रभावित नागरिकों को अब तक कोई स्पष्ट राहत या
मुआवजा नहीं मिला; उन्होंने निलंबित अधिकारियों के खिलाफ शीघ्र चार्जशीट और प्रभावितों को उचित मुआवजा व पुनर्वास
की माँग की है।

### घटनाक्रम (timeline — one case's chronology, **newest → oldest**)
1. **19 जुलाई, सुबह 7:58 बजे** — ग्रेटर जयपुर कॉरपोरेशन ने सात और अधिकारियों को निलंबित किया, लेकिन
   निलंबन के पीछे की प्रक्रिया और प्रभावित नागरिकों के अधिकारों पर सवाल बने रहे।
2. **18 जुलाई, दोपहर 1:47 बजे** — निलंबित अस्पतालों के कर्मचारियों और रोगियों ने उचित पुनर्वास व मुआवजे
   की माँग की, परंतु कोई स्पष्ट योजना नहीं बताई गई।
3. **17 जुलाई, रात 11:47 बजे** — राज्य सरकार ने आरजीएचएस के 51 अस्पतालों को अनियमितताओं के कारण निलंबित
   किया, जिससे ग्रामीण स्वास्थ्य सेवाओं में बाधा आई। *(टाइम्स ऑफ इंडिया)*
4. **10 जुलाई, रात 11:57 बजे** — एसीबी ने दो और अधिकारियों को रिश्वत के मामले में पकड़ लिया, जिससे
   भ्रष्टाचार की जाँच का दायरा बढ़ा। *(पंजाब केसरी)*
5. **6 जुलाई, दोपहर 12:30 बजे** — राजस्थान कृषि विभाग ने तीन अधिकारियों को निलंबित किया, परंतु निलंबन के
   बाद किसानों को कोई स्पष्ट राहत नहीं मिली।
6. **3 जुलाई, दोपहर 12:30 बजे** — जयपुर में कृषि विभाग के दो अधिकारियों से समान राशि बरामद की गई, जिससे
   विभागीय भ्रष्टाचार का पता चला। *(उदयपुर टाइम्स)*
7. **2 जुलाई, रात 9:22 बजे** — एसीबी ने दो सरकारी अधिकारियों से दो लाख तिरसठ हजार रुपये बरामद किए, जिससे
   भ्रष्टाचार के संकेत मिले। *(द प्रिंट)*

### मुख्य तथ्य (key facts — clean dated bullets)
- 2 जुलाई, रात 9:22 बजे: एसीबी ने दो सरकारी अधिकारियों से 2.63 लाख रुपये बरामद किए
- 3 जुलाई, दोपहर 12:30 बजे: एसीबी ने कृषि विभाग के दो अधिकारियों से 2.63 लाख रुपये बरामद किए
- 6 जुलाई, दोपहर 12:30 बजे: कृषि विभाग ने तीन अधिकारियों को निलंबित किया
- 10 जुलाई, रात 11:57 बजे: एसीबी ने दो अधिकारियों को रिश्वत मामले में पकड़ा
- 17-18 जुलाई: राजस्थान सरकार ने 51 आरजीएचएस अस्पतालों को अनियमितताओं के कारण निलंबित किया
- 19 जुलाई, सुबह 7:58 बजे: ग्रेटर जयपुर कॉरपोरेशन ने सात अधिकारियों को निलंबित किया

### पुलिस की जवाबदेही
एसीबी की कार्रवाई के बावजूद, पुलिस ने प्रारम्भिक रिपोर्टिंग और त्वरित जाँच में देरी की, जिससे कई मामलों में
भ्रष्टाचार के प्रमाण एकत्र करने में बाधा आई। निलंबित अधिकारियों के खिलाफ तुरंत चार्जशीट न बनाना और प्रभावित
नागरिकों को मुआवजा न देना, नागरिक समूहों के अनुसार, प्रशासनिक लापरवाही के संकेत हैं।

### आगे क्या
जाँच के आधार पर संबंधित अधिकारियों के खिलाफ चार्जशीट अपेक्षित है; विपक्ष और नागरिक समूहों ने प्रभावितों को
मुआवजा व पुनर्वास तथा दोषियों पर शीघ्र कार्रवाई की माँग की है।

### स्रोत (varied named outlets, each with a real Hindi title)
| आउटलेट | शीर्षक |
|--------|--------|
| द न्यू इंडियन एक्सप्रेस | एसीबी ने दो अधिकारियों से दो लाख तिरसठ हजार रुपये बरामद किए |
| उदयपुर टाइम्स | एसीबी ने कृषि विभाग के दो अधिकारियों से दो लाख तिरसठ हजार रुपये बरामद किए |
| टाइम्स ऑफ इंडिया | राजस्थान कृषि विभाग ने तीन अधिकारियों को निलंबित किया |
| पंजाब केसरी | एसीबी ने दो अधिकारियों को रिश्वत मामले में पकड़ा |
| ज़ूम न्यूज़ | राजस्थान सरकार ने 51 आरजीएचएस अस्पतालों को अनियमितताओं के कारण निलंबित किया |

### यह भी ब्रेकिंग
Accountability-only. A pro-government item — e.g. "जयपुर विकास प्राधिकरण ने अवैध इमारतों को ध्वस्त किया …
सुधार की उम्मीद" — is **not allowed** here.

### capture deltas (what the raw 19 Jul screenshot got wrong — learn to avoid)
- Rendered **ascending** under `घटनाक्रम — शुरुआत से अब तक`; the standard is `घटनाक्रम` (no "— शुरुआत से
  अब तक") and **descending** (newest first).
- Description ended with an **editorial** line ("सरकार … को यह स्पष्ट करना चाहिए …"); the standard
  **attributes** the demand to विपक्ष/नागरिक instead of the outlet prescribing.
- One स्रोत card was the pale "**ताज़ा रिपोर्ट**" and another had a garbled outlet label; the standard is
  varied **named** outlets, each with a real Hindi title.
- The यह भी ब्रेकिंग card was **pro-government** (JDA demolition, "सुधार की उम्मीद"); the standard gates
  that out.

---

## Use Case B — `इस महीने` (the month's different stories)

*Based on the 20 Jul 2026 output — a one-off Barmer patwari bribe as the lead, clubbing the month's
**unrelated** corruption/accountability cases. This is the current-render shape (descending, pulse on
top).*

### शीर्षक (title)
> बारमेर में पटवारी पर ₹15,000 की रिश्वत लेते पकड़े जाने से उजागर हुई राज्य-स्तर की भ्रष्टाचार की लहर,
> नागरिकों को न्याय और मुआवजे की मांग

*Current case (the bribe) → the month's wave ("राज्य-स्तर की भ्रष्टाचार की लहर") + the citizen angle
("न्याय और मुआवजे की मांग").*

### पूरी खबर (description — the lead first, then the month clubbed, attributed)
बारमेर में पटवारी द्वारा सीधे नागरिक से ₹15,000 की रिश्वत लेते पकड़े जाने की घटना (20 जुलाई, रात 9:19 बजे)
ने इस महीने राजस्थान में घटित कई भ्रष्टाचार और प्रशासनिक लापरवाही के मामलों को फिर से सामने लाया है। स्थानीय
प्रशासन ने तुरंत पटवारी को निलंबित कर दिया, परंतु नागरिकों ने पूछताछ में स्पष्ट जवाब नहीं मिलने और मुआवजे की
अनुपस्थिति पर सवाल उठाए हैं।

पिछले कुछ हफ्तों में राज्य के विभिन्न विभागों में भ्रष्टाचार, रिश्वत, प्रशासनिक लापरवाही और स्वास्थ्य
कुप्रबंधन के कई मामले सामने आए — 22 जून को पुलिस द्वारा पहलवान को मारना, 30 जून को बिजली लाइन टूटने से तीन
मौतें, 7 जुलाई को स्कूल मरम्मत फंड के ₹503 करोड़ की धोखाधड़ी के आरोप, 10 जुलाई को भूमि सीमांकन के लिए रिश्वत,
13 जुलाई को मंदिर दान स्कैम पर राजनीतिक टिप्पणी, 17 जुलाई को सी-सेक्शन के बाद एक महिला की मृत्यु और 19 जुलाई
को जोधपुर सरकारी अस्पताल में मातृ मृत्यु की स्थिति।

इन सभी घटनाओं में जिम्मेदार प्राधिकरण — पुलिस, नगर निगम, शिक्षा विभाग, भूमि विभाग, स्वास्थ्य विभाग — की चूक,
देरी या सक्रिय भ्रष्टाचार स्पष्ट है। नागरिकों ने शीघ्र जांच, चार्जशीट और प्रभावित परिवारों को मुआवजा व पुनर्वास
की मांग की है; विपक्ष ने इन मामलों को जोड़ते हुए राज्य सरकार की जवाबदेही पर सवाल उठाए हैं।

### इस महीने (the month's different Jaipur stories — **newest → oldest**, one line per story)
1. **20 जुलाई, रात 9:19 बजे** — बारमेर में पटवारी ने नागरिक से ₹15,000 की रिश्वत लेनी स्वीकार की और पकड़े
   जाने के बाद निलंबित किया गया। प्रभावित नागरिक अभी तक मुआवजे या पुनर्स्थापना की मांग कर रहे हैं।
2. **19 जुलाई, रात 12:25 बजे** — जोधपुर सरकारी अस्पताल में मातृ मृत्यु की स्थिति में पाँच नई माताओं को
   आईसीयू में स्थानांतरित किया गया। स्वास्थ्य विभाग ने जांच का वादा किया, परंतु अभी कोई आधिकारिक रिपोर्ट
   नहीं। *(टाइम्स ऑफ इंडिया)*
3. **17 जुलाई, शाम 4:45 बजे** — एक 25 वर्षीय महिला का सी-सेक्शन के बाद निधन; परिवार ने अस्पताल पर लापरवाही
   का आरोप लगाया। डॉक्टरों की अनुपस्थिति और उपकरणों की खराबी की रिपोर्टें सामने आईं।
4. **13 जुलाई, शाम 6:35 बजे** — एक प्रमुख मंदिर दान स्कैम के आरोपों को झूठा ठहराया गया, जबकि 1,100 पृष्ठों
   के दस्तावेज़ और ₹22 करोड़ के लेन-देन का उल्लेख। विरोधी दल ने इसे राजनीतिक दबाव कहकर सवाल उठाए।
5. **10 जुलाई, शाम 6:47 बजे** — भूमि विभाग के दो अधिकारी जमीन के पुनः सीमांकन के लिए रिश्वत लेते पकड़े गए;
   निलंबित किए गए, परंतु कानूनी प्रक्रिया अभी शुरू नहीं हुई।
6. **7 जुलाई, सुबह 4:18 बजे** — ड्रोन सर्वेक्षण ने स्कूल मरम्मत फंड के ₹503 करोड़ की धोखाधड़ी के संकेत
   दिखाए; छतों के ढहने और फर्जी रिकॉर्ड की पुष्टि, परंतु कोई जांच रिपोर्ट सार्वजनिक नहीं।
7. **30 जून, दोपहर 12:30 बजे** — विद्युत विभाग की लाइन टूटने से तीन लोग मारे गए। परिवारों ने सुरक्षा उपायों
   की कमी पर विरोध किया, परंतु विभाग ने कोई क्षतिपूर्ति का उल्लेख नहीं किया।
8. **22 जून, रात 9:43 बजे** — जयपुर पुलिस के एक सब-इंस्पेक्टर द्वारा पहलवान पर शारीरिक हमले की पुष्टि हुई;
   पीड़ित गंभीर चोटों के साथ अस्पताल में, जबकि पुलिस ने कोई अनुशासनात्मक कार्रवाई नहीं की।

*8 distinct cases across the month — **clubbed**, not chained. Each is a separate incident with its own
date + (where known) outlet, newest on top.*

### मुख्य तथ्य (key facts — one clean bullet per case: who · department · amount · action)
- बारमेर पटवारी: ₹15,000 रिश्वत बरामद, निलंबित — अभी तक आगे कोई कार्रवाई नहीं
- जयपुर पुलिस सब-इंस्पेक्टर: पहलवान पर शारीरिक हमला, पीड़ित अस्पताल में — कोई अनुशासनात्मक कार्रवाई नहीं
- राज्य विद्युत विभाग: लाइन टूटने से तीन मौतें, सुरक्षा लापरवाही — कोई क्षतिपूर्ति नहीं
- शिक्षा विभाग: स्कूल मरम्मत फंड में ₹503 करोड़ की धोखाधड़ी (ड्रोन से उजागर) — जांच रिपोर्ट अभी नहीं
- भूमि विभाग: दो अधिकारी सीमांकन के लिए रिश्वत लेते पकड़े, निलंबित — कानूनी प्रक्रिया अभी नहीं
- स्वास्थ्य विभाग: सी-सेक्शन के बाद महिला की मृत्यु, चिकित्सा लापरवाही — परिवार का विरोध

### पुलिस की जवाबदेही
जयपुर में पुलिस अधिकारी द्वारा पहलवान पर शारीरिक हमले की घटना में पुलिस ने तुरंत आरोपी को निलंबित नहीं किया,
जिससे नागरिक सुरक्षा पर गंभीर सवाल उठे। बारमेर में स्थानीय प्रशासन ने त्वरित निलंबन किया, परंतु पुलिस द्वारा
रिश्वत-भुगतान की जांच अभी पूरी नहीं हुई।

### आगे क्या
रिपोर्टेड मामलों की स्वतंत्र जांच और चार्जशीट की मांग विपक्ष और नागरिक समूहों ने दोहराई है; प्रभावित परिवारों
को मुआवजा और पुनर्वास की तत्काल व्यवस्था की भी माँग की जा रही है।

### स्रोत (varied named outlets, each with a real Hindi title)
| आउटलेट | शीर्षक |
|--------|--------|
| जयपुर न्यूज़ | बारमेर पटवारी पर रिश्वत लेने का मामला |
| टाइम्स ऑफ इंडिया | राजस्थान में मातृ मृत्यु का डर: जोधपुर सरकारी अस्पताल में सी-सेक्शन के बाद 5 माताएँ आईसीयू में |
| जयपुर न्यूज़ | जोधपुर अस्पताल में संभावित चिकित्सा लापरवाही से गंभीर स्थिति में महिला |
| जयपुर न्यूज़ | 25 वर्षीया महिला की सी-सेक्शन के बाद मृत्यु, परिवार ने विरोध किया |
| द प्रिंट | राजस्थान: डॉक्टर पर महिला के गर्भपात के बाद प्रक्रिया के लिए रिश्वत लेने का आरोप |

### capture deltas (what the raw 20 Jul screenshot got wrong — learn to avoid)
- मुख्य तथ्य were comma-joined **field-dumps** ("बारमेर पटवारी, पटवारी, ₹15,000, रिश्वत, बरामद, …"); the
  standard is a clean **natural bullet per case** (as above), not a telegraphic field list.
- One timeline step showed a bare, amount-less "**₹ रुपए की राशि**" (a dropped number); the standard:
  if the amount is unknown, omit it or write "अज्ञात राशि" — never a bare ₹ with no figure.

---

## Standards checklist (the rules, per section — apply to BOTH use cases)

- **Title** — hard news on the *current* story (and the month's aggregate in month mode);
  foregrounds the newest/biggest development; factual and sensation-free; Devanagari only.
- **Description (पूरी खबर)** — hard news, **inverted pyramid** (newest development first), **attributed**
  (पुलिस/विपक्ष/नागरिकों के अनुसार); clubs the month's stories where relevant; **no** editorial "सरकार को …
  करना चाहिए", **no** taking sides for or against any party/authority.
- **Timeline section (two modes)** — pick `घटनाक्रम` (one story's chronology) vs `इस महीने`
  (the month's different stories, one distinct story per step) per the decision table above. Either way:
  **≥5 steps, descending (newest first), scroll-triggered reveal, pulse on top item**, real date label +
  reporting outlet, 2–3 sentence sourced text. **Never** raw data dumps (`[{'_': …}]`, joined arrays),
  stray `:` / `–`, or a bare `₹` with no figure.
- **Key facts (मुख्य तथ्य)** — clean dated/natural bullets (who, department, amount, action); **not**
  comma-joined field lists.
- **Sources (स्रोत)** — **varied, named outlets** each with a **real Hindi title**; never the pale
  "ताज़ा रिपोर्ट" on every card.
- **यह भी ब्रेकिंग** — the day's other fresh, non-ceremonial Jaipur stories (`order_secondary`);
  higher-impact/accountability stories front-loaded.
- **Global** — fully **Devanagari** (`to_hindi`; acronyms → जेडीए/भाजपा/ईडी/एसीबी…); **no fabrication**
  (only sourced facts + attributed questions; no invented amounts/allegations about named people); no
  field-name/bracket tags (`(analysis)`, `(lead_story)`); the request stays within the **Groq TPM
  budget** (check with `python breaking/check_tpm.py`).

## How this maps to the code
- Mode pick → `build()` (`own_pts` vs `SINGLE_CASE_MIN`), heading/note in `state` →
  `{{TIMELINE_HEADING}}` / `{{TIMELINE_NOTE}}`.
- Clubbed month timeline → `month_story_arc()` (aggregates the month's archive points);
  single-case arc → `_arc_sample(own_pts, TIMELINE_MAX)`.
- Descending order + first-item pulse + scroll reveal → `render()` (reversed developments) + the
  `.tl-item` / `IntersectionObserver` CSS+JS in `PAGE_TEMPLATE`.
- Varied titled sources → `arc_sources()` + the AI's `sources_hi`, with `HINDI_SOURCE` covering the
  common outlets.
- Devanagari + no raw dumps → `to_hindi` / `_ai_str` / `_ai_str_list` in `_lead_from_ai`.
- Lead pick → `apply_lead` (top fresh non-ceremonial cluster, topic-neutral score); secondary pool →
  `order_secondary` (the day's other fresh, non-ceremonial Jaipur stories by score).
- Hard-news, attributed, mode-aware voice → the `_groq_messages` prompt (`timeline_mode`).

## Machine-readable copy
Both use cases are also in **`breaking-cases.json`** (structured `title` / `description` /
`timeline` / `key_facts` / `sources` / `capture_deltas` per case) — a **reference**, not a runtime
input: do **not** feed it into the Groq prompt (it would blow the 8000 TPM budget). Use it when
adapting the prompt or reviewing output.
