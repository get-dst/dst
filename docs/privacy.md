# Privacy

This page covers www.dataservetool.com and the public demo at demo.dataservetool.com.
dst itself is software you run yourself; when you do, none of this applies and nothing
reaches us (see [Security and data flow](security.md)).

## The website

The documentation site sets no cookies, runs no analytics and collects nothing about
visitors beyond the standard request logs of its host, Google Cloud.

## The demo

To use the demo you sign in with Google, GitHub, Discord or an email code. Sign-in is
handled by [Clerk](https://clerk.com); the demo receives your email address from it and
uses that address as your caller name.

What the demo keeps:

- **Your email address**, as the name on your API key and on each question you ask.
- **Each question you ask and the answer it got**, with its timing and cost, in the
  demo's request log. These are reviewed to improve the product, and deleted
  automatically after 30 days.
- **One API key per account**, valid for 7 days. Minting a new one retires the old one.

Where it goes:

- The demo runs on Google Cloud in Belgium (europe-west1): the service, its database and
  the request log.
- The text of each question is sent to the model providers that produce the answer,
  [DeepSeek](https://www.deepseek.com) and [TypeSafe](https://typesafe.ai).
- The data the demo answers from is public Dota 2 match data from
  [OpenDota](https://www.opendota.com), held in MotherDuck. It contains no data about you.

The demo does not sell or share your data, shows no ads, and uses no tracking cookies.
Clerk sets the cookies that keep you signed in.

## Your data

To have your questions and your account deleted before the 30 days are up, or to ask
what is held about you, write to security@dataservetool.com from the address you signed
in with.
