# comftp

## What is it?

Simple example of implementing [aioftp](https://github.com/pohmelie/aioftp) path io layer for remote FreeDOS console via serial port (rs232). It is designed for [fastwel cpc304](http://www.fastwel.com/products/pc-104-cpu-boards/cpc304/) platform, but should work for any dos-compatible (of course there should be xmodem transfer utility).

## What a hell?!

It is more proof of concept and toy, but, AFAIK DOS have no pretty file exchange ability.

## How does it work

FreeDOS have some path io commands (dir, md, copy, etc.) they aplied via serial console, response is parsed. Sending/receiving files feature released via xmodem protocol and FreeDOS-side utility ftrans (anyway, there can be anything you want).

## Requirements

* python 3.4.2+
* aioftp 0.2.0+
* pyserial 2.7.0+

For more help type `comftp --help`
