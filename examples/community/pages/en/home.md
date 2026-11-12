---
title: Welcome
description: Where to pay in bitcoin, who accepts it and how to start.
---
<div class="showcase">
<img src="/theme/{{ community.logo }}" alt="" width="140" height="140">
<div>
<p class="eyebrow">Welcome</p>
<h1>{{ community.name }}</h1>
<p>{{ tagline }}</p>
</div>
</div>

**{{ community.name }}** is a group of people who pay and get paid in bitcoin, in and around {{ community.city }}. This sentence is yours to rewrite: say who you are and why you do this, in your own words.

<div class="stats" markdown="1">
<div class="stat" markdown="1"><strong>{{ stats.shops }}</strong> shops on the map</div>
<div class="stat" markdown="1"><strong>{{ stats.discounts }}</strong> with a discount</div>
</div>

## The first stops

Three places to start from. You change them in `community.yml`.

{% include "website/route.html" %}

## Got a shop?

The shopkeeper chooses how to get paid, and the money goes straight to their own wallet: the community holds no funds and no keys.

[Start here](/en/shops/){.button}
