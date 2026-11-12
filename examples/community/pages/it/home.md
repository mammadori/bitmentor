---
title: Benvenuti
description: Dove pagare in bitcoin, chi accetta e come cominciare.
---
<div class="showcase">
<img src="/theme/{{ community.logo }}" alt="" width="140" height="140">
<div>
<p class="eyebrow">Benvenuti</p>
<h1>{{ community.name }}</h1>
<p>{{ tagline }}</p>
</div>
</div>

**{{ community.name }}** è un gruppo di persone che pagano e incassano in bitcoin, tra {{ community.city }} e dintorni. Questa frase è da riscrivere: di' chi siete e perché lo fate, con parole vostre.

<div class="stats" markdown="1">
<div class="stat" markdown="1"><strong>{{ stats.shops }}</strong> negozi sulla mappa</div>
<div class="stat" markdown="1"><strong>{{ stats.discounts }}</strong> con uno sconto</div>
</div>

## Le prime tappe

Tre posti da cui cominciare. Si cambiano in `community.yml`.

{% include "website/route.html" %}

## Hai un negozio?

Il commerciante sceglie da solo come incassare, e i soldi vanno direttamente sul suo wallet: la comunità non custodisce fondi né chiavi.

[Comincia da qui](/it/shops/){.button}
