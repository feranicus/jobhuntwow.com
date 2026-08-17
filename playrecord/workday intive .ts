Record









Target:

Test Runner



3
test('test', async ({ page }) => {
4
  await page.goto('https://intive.wd3.myworkdayjobs.com/Intive/job/Remote-Germany/Chief-Commercial-Officer---General-Manager---EMEA_JR101912?source=LinkedIn');
5
  await page.getByRole('button', { name: 'Apply' }).click();
6
  await page.getByRole('button', { name: 'Autofill with Resume' }).click();
7
  await page.getByRole('button', { name: 'Accept Cookies' }).click();
8
  await page.getByRole('button', { name: 'Select file' }).click();
9
  await page.locator('input[type="file"]').setInputFiles('Evgeny Vainshtein - CV 2026.pdf');
10
  await page.getByRole('button', { name: 'Next' }).click();
11
  await page.getByRole('button', { name: 'How Did You Hear About Us?' }).click();
12
  await page.getByText('LinkedIn corporate page').click();
13
  await page.getByRole('radio', { name: 'No' }).check();
14
  await page.getByRole('button', { name: 'Country Georgia Required' }).click();
15
  await page.getByText('Germany').click();
16
  await page.getByRole('textbox', { name: 'Street' }).click();
17
  await page.getByRole('textbox', { name: 'Street' }).fill('Herman J Bach Weg 16');
18
  await page.getByRole('textbox', { name: 'Postal Code' }).click();
19
  await page.getByRole('textbox', { name: 'Postal Code' }).fill('61169');
20
  await page.getByRole('textbox', { name: 'City' }).dblclick();
21
  await page.getByRole('textbox', { name: 'City' }).fill('Friedberg');
22
  await page.getByRole('textbox', { name: 'Phone Number' }).click();
23
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
24
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
25
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
26
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
27
  await page.getByRole('textbox', { name: 'Phone Number' }).fill('+49 157 8551545');
28
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
29
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
30
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
31
  await page.getByRole('textbox', { name: 'Phone Number' }).fill('+49 1578551545');
32
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
33
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
34
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowLeft');
35
  await page.getByRole('textbox', { name: 'Phone Number' }).fill('1578551545');
36
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowRight');
37
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowRight');
38
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowRight');
39
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowRight');
40
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowRight');
41
  await page.getByRole('textbox', { name: 'Phone Number' }).press('ArrowRight');
42
  await page.getByRole('textbox', { name: 'Phone Number' }).fill('15785541545');
43
  await page.getByRole('button', { name: 'Next' }).click();
44
  await page.getByRole('button', { name: 'Next' }).click();
45
  await page.getByRole('button', { name: 'Calendar' }).click();
46
  await page.getByRole('button', { name: 'Next month' }).click();
47
  await page.getByRole('button', { name: 'Thursday 17 September' }).click();
48
  await page.locator('#primaryQuestionnaire--e55467596803100100f34ed769170000').click();
49
  await page.locator('#primaryQuestionnaire--e55467596803100100f34ed769170000').fill('130k base + 70k bonus + Car');
50
  await page.getByRole('button', { name: 'Select One', exact: true }).click();
51
  await page.getByRole('option', { name: 'Yes' }).click();
52
  await page.getByRole('button', { name: 'Select One Required' }).click();
53
  await page.getByRole('listbox').getByText('Yes').click();
54
  await page.getByRole('button', { name: 'Next' }).click();
55
  await page.getByRole('checkbox', { name: 'I acknowledge and want to' }).check();
56
  await page.getByRole('button', { name: 'Next' }).click();
57
  await page.getByRole('button', { name: 'Submit' }).click();
58
  await page.getByRole('button', { name: 'Close' }).click();
59
});


Type locator to inspect
​
