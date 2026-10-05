# ac.ticket_comment.browse  (requirements: requirement.ticket_comments; operations: operation.ticket_comment.list)
Feature: Browse Comments succeeds

  Scenario: Browse Comments succeeds
    Given the user is signed in as Customer or Agent or Manager
    When List Comments
    Then The Comment information is displayed
